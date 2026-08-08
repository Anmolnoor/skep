"""Chat tools (v6 Stage D): what the Queen's model may see and may propose.

Two tiers, one rule. Read tools execute immediately inside the turn — the
model can always look. Mutating tools NEVER execute from the model's hand:
each call becomes a ``chat_actions`` row and a confirm-card in the UI, and
runs only when the human clicks confirm — through the same ``actions.py``
verbs as the buttons in the Approvals/Policies views, under actor
``chat-user``. The model never holds the trigger, except for trusted
``dispatch_run`` calls that match a project's default policy exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, get_args

from fastapi import HTTPException

from ..autonomy import AutonomyDecision
from ..castes import CASTES, caste_names
from ..engines import engine_names
from ..memory import MEMORY_CLASSES, MemoryError
from ..projects import list_projects, project_to_dict
from ..providers import PROVIDER_COST_CLASSES, PROVIDER_PROTOCOLS
from ..store import ChatSearchHit, RunStore
from . import actions
from .jobs import Dispatcher
from .llm import LLMProtocol
from .registry import (
    known_repos,
    register_repo,
    repos_root,
    resolve_repo_arg,
    set_project_phase,
    setup_project_record,
)
from .remediation import remediation_for
from .settings import (
    DEFAULT_TICKER_INTERVAL,
    TICKER_INTERVAL_SECONDS,
    ConfigHolder,
    policy_view,
)

CHAT_TOOL_ACTOR = "chat-user"
FAILED_RUN_STATES = {"failed", "rejected", "worker_timeout", "worker_crashed"}

# v51-F3: run_code's budget and result bounds. The wall clock matches the
# v44-F4 script-schedule lane; the output cap keeps a chatty script from
# flooding the transcript (full output stays in the run's output artifact).
SCRIPT_RUN_WALL_CLOCK_SECONDS = 120
# v106-F7: the per-call ceiling. Two field scripts driving npm died at exactly
# 120s — the default stays right for smoke scripts, but a caller who KNOWS the
# work is slow may ask for up to this much.
SCRIPT_RUN_MAX_WALL_CLOCK_SECONDS = 600
SCRIPT_RUN_OUTPUT_CAP = 4000
_SCRIPT_RUN_POLL_SECONDS = 0.1

# v51-F5 (ADR 0025): batch dispatch is N independent governed runs submitted
# together, not a new execution model. The cap matches Hermes's batch size.
BATCH_DISPATCH_CAP = 3
# v83-F7 (ADR 0041): reasoning-only analysts per delegate_analysis call — a
# separate resource class from the worker cap above (raising either is its
# own policy question).
ANALYSIS_CAP = 3


def _search_hit_payload(hit: ChatSearchHit) -> dict[str, Any]:
    """v84-F8 (A5/I8): the provenance marker rides IN the text the model
    reads — a store-only marker is invisible at the moment imported words
    are being trusted as the operator's own."""
    payload = asdict(hit)
    if hit.source == "hermes-import":
        payload["snippet"] = f"[hermes-import] {payload['snippet']}"
    return payload


def _script_run_result(
    store: RunStore, task_id: str, *, wall_clock_seconds: int = SCRIPT_RUN_WALL_CLOCK_SECONDS
) -> dict[str, Any]:
    """Block until the script run finishes, then return its output.

    stdout/stderr ride the run's command.result event (full text stays in the
    audit trail and the output artifact); the tool result carries a capped
    copy so the model's context and the transcript stay bounded.
    """
    import time

    deadline = time.monotonic() + wall_clock_seconds + 30
    state = ""
    while time.monotonic() < deadline:
        record = store.get_run(task_id)
        state = "" if record is None else str(record.state)
        if state == "completed" or state in FAILED_RUN_STATES:
            break
        time.sleep(_SCRIPT_RUN_POLL_SECONDS)
    else:
        return {
            "task_id": task_id,
            "state": state,
            "error": "the script run did not reach a terminal state in time",
        }

    def _capped(text: str) -> str:
        return (
            text
            if len(text) <= SCRIPT_RUN_OUTPUT_CAP
            else (text[:SCRIPT_RUN_OUTPUT_CAP] + "\n… (truncated; full output in the run artifact)")
        )

    result: dict[str, Any] = {"task_id": task_id, "state": state}
    for event in actions.current_events(store, task_id):
        if event.type.value != "command.result":
            continue
        payload = event.payload
        result["exit_code"] = payload.get("exit_code")
        result["output"] = _capped(str(payload.get("stdout") or ""))
        stderr = str(payload.get("stderr") or "")
        if stderr:
            result["stderr"] = _capped(stderr)
    record = store.get_run(task_id)
    if record is not None and record.summary:
        result["summary"] = record.summary
    # v81-F6: point at the delivered files — "completed" with the deliverable
    # stranded in a destroyed worktree is success with nothing to show.
    delivery = None
    delivered_files: list[str] = []
    for kind, path, _ in store.artifacts_for(task_id):
        if kind == "workspace_delivery":
            delivery = path
        elif kind == "file" and Path(path).name != "output.txt":
            delivered_files.append(Path(path).name)
    if delivery is not None:
        result["delivered_to"] = delivery
        result["delivered_files"] = delivered_files
    return result


def _forge_trial(
    store: RunStore,
    holder: ConfigHolder,
    runner: Dispatcher,
    *,
    source: str,
    repo: str,
    decision: AutonomyDecision | None,
) -> dict[str, Any]:
    """v71-F1: the forged tool's sandboxed trial — exactly the run_code lane
    (script caste, sandbox, deny-all egress), so plugin_can_run's
    sandboxed_no_network shape is literally what runs. Tests monkeypatch this
    seam instead of standing up the dispatch pipeline."""
    from skep.workers.script_worker import script_instructions

    from ..forge import trial_script

    task_id = actions.submit_run(
        holder,
        runner,
        store,
        repo=repo,
        instructions=script_instructions("python", trial_script(source)),
        caste="script",
        execution_mode="sandbox",
        network=[],
        wall_clock_seconds=SCRIPT_RUN_WALL_CLOCK_SECONDS,
        dispatch_decision=decision,
    )
    return _script_run_result(store, task_id)


# v25-F1: the command deck's mutating verbs — operator-typed /commands the UI
# turns into confirm-carded chat actions (source 'operator'). COMMAND_ONLY
# tools are executable through execute_mutation but never offered to the model.
COMMAND_ONLY_TOOLS = frozenset({"set_project_phase"})

# v51-F7: a THIRD interaction type — not a read, not a mutation. The chat
# engine intercepts this call, posts the question as a normal assistant
# message (every face renders it for free), and ENDS the turn; the user's
# next message is the answer. The chat's own turn cycle is the pause — no
# card, no actor, no new state machine. It lives in READ_TOOL_SPECS so the
# model sees it; execute_read_tool only carries the non-chat fallback.
CLARIFY_TOOL_NAME = "ask_clarifying_question"
COMMAND_TOOL_NAMES = (
    frozenset(
        {
            "setup_project",
            "land_run",
            "approve_review",
            "deny_review",
            "workon",
            "propose_schedule",
            "set_personality",
            "set_persona",
            # v73-F2: crash recovery must not depend on a working model —
            # provider trouble is exactly when runs crash.
            "resume_run",
            # v77-F3: the terminal /model command cards the brain dial the
            # same way the chat tool does — one verb, three faces.
            "set_assistant_model",
            # v83-F11: /browser registers the Playwright MCP server once.
            "setup_browser",
            # v96-F4: the composer's Push / Open PR buttons — the verbs
            # existed carded since v47/v57; only this allowlist stood between
            # them and the operator-command resolution path. Both stay
            # web-UI-only (absent from CHANNEL_CONFIRMABLE_ACTIONS).
            "push_branch",
            "open_pr",
            # v97-F6 (acceptance find — the v96-F4 lesson relearned): the
            # group verbs must pass this gate for operator-proposed cards.
            "set_policy_group",
            "delete_policy_group",
            "attach_policy_group",
            "detach_policy_group",
            # v110-F2: the deck's /sync — proposes running the operator's
            # pinned fleet sync command; web-UI-only like push_branch.
            "sync_fleet",
        }
    )
    | COMMAND_ONLY_TOOLS
)


def _optional_int(args: dict[str, Any], key: str) -> int | None:
    if key not in args or args[key] is None:
        return None
    return int(args[key])


def _object_arg(args: dict[str, Any], key: str) -> dict[str, Any]:
    """An object-typed tool param, tolerating the JSON-string variant.

    Small chat models routinely stringify nested objects
    (``"policy_overrides": "{\\"coding_engine\\": ...}"``); the string rides
    the stored card args to confirm time, so decode here — the one place all
    mutation paths consume them — and refuse honestly instead of a 500.
    """
    value = args.get(key)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must be a JSON object, got unparseable string") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    return {str(k): v for k, v in value.items()}


def _caste_guidance() -> str:
    """v101-F12: the per-caste guidance, generated from the registry.

    The enums here hardcoded two and three castes, so even after v101-F2/F3 the
    Queen could not ask for a verifier, a reviewer, a researcher or a script
    run. Worse than unreachable: CLAUDE.md's standing rule is that the Queen
    runs a small model and skims tool descriptions, so a schema omitting half
    the roster trains it never to use them — the exact drift recorded as having
    caused repeated "hallucinations".

    Hand-written prose about castes is the same defect one layer up: a new caste
    ships and the description is silently stale. This reads ``summary`` — the
    one string the Settings roster (F9) and the Assign field help (F10) also
    read, so the operator and the model are told the same thing in the same
    words.
    """
    return "which worker runs this. " + " ".join(
        f"{name}: {CASTES[name].summary}" for name in caste_names()
    )


def _tool(
    name: str, description: str, params: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required or [],
            },
        },
    }


READ_TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "list_mcp_servers",
        "Registered MCP servers (id, transport, command/url). MCP tools are "
        "policy-gated: read-shaped tools run free, everything else needs a "
        "confirmation or a learned allow rule.",
        {},
    ),
    _tool(
        "list_mcp_tools",
        "Discover the tools one registered MCP server offers, with each "
        "tool's risk class and what the current policy would decide for it.",
        {"server_id": {"type": "string"}},
        ["server_id"],
    ),
    _tool(
        "list_runs",
        "Recent runs, newest first: state, verification, summary.",
        {"limit": {"type": "integer", "description": "max runs to return (default 10)"}},
    ),
    _tool(
        "get_run",
        "One run in detail: state transitions, commands, approvals, verification.",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _tool(
        "list_approvals",
        "The pending approval queue, with each run's summary — PLUS the last "
        "10 resolved verdicts (recently_resolved: who approved/denied, when, "
        "landing branch). If the queue is empty, check recently_resolved "
        "before concluding nothing happened: a gate approved from the web UI "
        "resolves there, and the approved run resumes under a NEW task_id "
        "(follow the run's resumed_as pointer).",
        {},
    ),
    _tool(
        "get_policy",
        "The STORED supervisor policy (autonomy + defaults). For what a specific "
        "repo's next run will actually get — and why a command gated — use "
        "effective_policy instead.",
        {},
    ),
    _tool(
        "effective_policy",
        "What a run against this repo will ACTUALLY get: resolved project (or "
        "'global defaults'), execution mode, the shell allowlist as the worker "
        "sees it, network post-provider-merge, and the landing posture. Check "
        "this FIRST when a command was gated or before dispatching to a repo.",
        {"repo": {"type": "string", "description": "repo slug or path"}},
        ["repo"],
    ),
    _tool("list_templates", "Saved workflow templates (hand-authored and learned).", {}),
    _tool("list_skills", "Learned-skill candidates and skill-pack ladder states.", {}),
    _tool(
        "list_plugins",
        "Forged plugins — MCP tool servers skep authored for itself — with "
        "lifecycle state (draft/sandboxed/tested/approved/active/suspended/"
        "rolled_back), the authoring run, its landing branch, and the last "
        "trial evidence. registered=true means the tool is callable via "
        "call_mcp_tool right now. Check here (and list_mcp_tools) before "
        "proposing forge_tool, so nothing is forged twice.",
        {},
    ),
    _tool(
        "view_skill",
        "Show one saved skill/template's full recipe: instructions, caste, "
        "params, provenance, and its capability grants. Names come from "
        "list_templates (approved learned skills live there too; list_skills "
        "shows the still-in-pipeline candidates).",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "list_schedules",
        "Recurring schedules the ticker dispatches — a compact list: name, "
        "caste, enabled, interval, last/next run, last_state. Two-step: list "
        "first, then pass name=<schedule> to get ONE schedule in full detail "
        "(instructions/recipe, last_output, project context) — the schedule "
        "itself holds the recipe; never hunt old chats for it. To execute one "
        "immediately, propose run_schedule_now.",
        {"name": {"type": "string", "description": "one schedule's name for full detail"}},
    ),
    _tool(
        "list_repos",
        "Every repo runs can target: managed clones (target by slug) AND "
        "workon-bound local directories (target by path) — each entry carries "
        "name, path, and source ('clone' or 'workon'). If a directory the "
        "user names is missing here, it is not registered: offer workon (local "
        "dir) or register_repo (git URL) instead of dispatching at it.",
        {},
    ),
    _tool(
        "repo_state",
        "A repo's git state: checked-out branch, local and remote branches with "
        "tips, freshness (last_fetched, behind_origin), recent default-branch "
        "commits. Check this BEFORE dispatching work that mentions a branch — the "
        "branch (or the work itself) may already exist. This reads the LOCAL "
        "clone; if something pushed to the remote is missing, refresh_repo first.",
        {"repo": {"type": "string", "description": "repo slug or path"}},
        ["repo"],
    ),
    _tool(
        "git_log",
        "Recent commits on a ref (LOCAL clone; local branch, origin/<branch>, or a "
        "rev), oldest last omitted — one line each. Defaults to the repo's default "
        "branch, up to 50. If a ref pushed to the remote is unknown, refresh_repo "
        "first.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "ref": {"type": "string", "description": "branch, origin/<branch>, or rev"},
            "count": {"type": "integer", "description": "how many commits (1-50, default 20)"},
        },
        ["repo"],
    ),
    _tool(
        "git_diff",
        "What changed between two refs: --stat lines plus capped patch text "
        "(honest truncation marker). Defaults to <default branch>...HEAD. Use to "
        "review a landing branch (base=main, head=skep/<task_id>) before "
        "approving, landing, or opening a PR. LOCAL clone — refresh_repo first "
        "if the remote moved.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "base": {"type": "string", "description": "base ref (default: default branch)"},
            "head": {"type": "string", "description": "head ref (default: HEAD)"},
        },
        ["repo"],
    ),
    _tool(
        "list_worktrees",
        "The repo's live git worktrees joined with skep's runs: the main clone "
        "plus one detached worktree per in-flight task (and reverify-<id> during "
        "re-verification), each with path, HEAD, branch, and the run's state. "
        "THE answer to 'what is skep physically working on right now?'.",
        {"repo": {"type": "string", "description": "registered repo slug or host path"}},
        ["repo"],
    ),
    _tool(
        "list_prs",
        "GitHub pull requests for a registered repo (number, title, state, head "
        "branch, url, draft flag) — read-only, via gh on the operator's own "
        "credentials. Honest failure when gh is missing or unauthenticated. Use "
        "before open_pr (does one already exist?) and before merge_pr.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "state": {"type": "string", "enum": ["open", "closed", "merged", "all"]},
        },
        ["repo"],
    ),
    _tool("list_projects", "Registered trusted projects and their bound strategy/phase.", {}),
    _tool(
        "list_providers",
        "Registered LLM provider profiles. Shows id, protocol, endpoint, "
        "model, cost class, active flag, provenance.",
        {},
    ),
    _tool(
        "list_provider_presets",
        "Provider preset catalog. Each row names protocol, endpoint, key "
        "env var, default model, and what leaves the machine; feed its "
        "preset_id to add_provider.",
        {},
    ),
    _tool(
        "list_policy_groups",
        "Named policy groups (reusable convenience-grant bundles: network hosts, "
        "shell prefixes, env vars, budgets, engine) with the projects each is "
        "attached to. Builtins: python-bootstrap (uv/pip install + pypi), "
        "node-dev (npm install/ci + registry.npmjs.org). Groups compose live "
        "into run policy for every attached project.",
        {},
    ),
    _tool(
        "list_notes",
        "Saved notes, newest first — a page of them, with the total count. "
        "An older note that is not in the page still exists: page with offset.",
        {
            "limit": {"type": "integer", "description": "page size (default 20)"},
            "offset": {"type": "integer", "description": "skip this many newest notes"},
        },
    ),
    _tool("list_tasks", "Saved tasks with status and due-state.", {}),
    _tool(
        "add_note",
        "Add an inert note immediately. This does not schedule behavior.",
        {"content": {"type": "string"}},
        ["content"],
    ),
    _tool(
        "add_task",
        "Add an inert todo task immediately. Do not include due dates here.",
        {"title": {"type": "string"}},
        ["title"],
    ),
    _tool(
        "complete_task",
        "Mark a task done immediately.",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _tool(
        "reopen_task",
        "Set a done task back to todo immediately — the undo for a wrong "
        "complete_task (e.g. a landing that actually failed). Never add a "
        "duplicate todo to reopen work.",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _tool(
        "list_memory",
        "Durable curated memory items (optionally scoped to a project).",
        {"project": {"type": "string", "description": "optional project id"}},
    ),
    _tool(
        "search_memory",
        "Full-text search durable curated memory.",
        {
            "query": {"type": "string"},
            "project": {"type": "string", "description": "optional project id"},
        },
        ["query"],
    ),
    _tool(
        "list_memory_proposals",
        "Curated-memory proposals awaiting review (optionally filter by state). "
        "Proposals are created with `skep memory propose --from-note <id> "
        "--class <class>`; valid classes are "
        # v49-F4: enumerate from the real constant so this cannot drift.
        f"{', '.join(sorted(MEMORY_CLASSES))}. "
        "Tell the user the command and classes when they want a note promoted "
        "to durable memory.",
        {"state": {"type": "string", "description": "e.g. pending_review"}},
    ),
    _tool(
        CLARIFY_TOOL_NAME,
        "Ask the user ONE structured question when you are blocked on a "
        "choice only they can make. Call it ALONE — the turn ends with the "
        "question, and the user's NEXT MESSAGE is the answer. choices render "
        "as clickable buttons in the web UI and numbered options everywhere "
        "else. Never use it for something a read tool can answer.",
        {
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        },
        ["question"],
    ),
    _tool(
        "search_chats",
        "Full-text search past chat transcripts, best match first: chat title, "
        "role, time, and a snippet with the match in [brackets]. Use when the "
        "user references an earlier conversation ('what did we decide last "
        "week?'); search_memory covers curated memory, this covers what was "
        "actually said. Pass chat_id to search inside ONE conversation; "
        "get_chat_context reads the exchange around a hit.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "max messages to return (default 20)"},
            "chat_id": {"type": "string", "description": "optional: scope to one chat"},
        },
        ["query"],
    ),
    _tool(
        "list_chats",
        "Browse recent conversations: chat_id, title, timestamps, message "
        "count, and which face opened it (web/terminal/channel). Use for "
        "'what have we talked about recently?'; search_chats finds content, "
        "this lists sessions.",
        {"limit": {"type": "integer", "description": "max chats to return (default 10)"}},
    ),
    _tool(
        "get_chat_messages",
        "Read one past conversation's messages in order (paginated) — the "
        "scroll tool: see the context around a search_chats hit or replay a "
        "whole past chat. Long messages are truncated.",
        {
            "chat_id": {"type": "string"},
            "limit": {"type": "integer", "description": "max messages (default 20, max 50)"},
            "offset": {"type": "integer", "description": "skip this many messages first"},
        },
        ["chat_id"],
    ),
    _tool(
        "get_chat_context",
        "Read the messages AROUND one message in a past conversation — the "
        "follow-up to a search_chats hit: pass the hit's chat_id and "
        "message_id to see the surrounding exchange without replaying the "
        "whole chat. Each row carries its id, so page further by calling "
        "again anchored on the first or last row.",
        {
            "chat_id": {"type": "string"},
            "message_id": {"type": "integer", "description": "the anchor message id"},
            "before": {
                "type": "integer",
                "description": "messages before the anchor (default 10, max 25)",
            },
            "after": {
                "type": "integer",
                "description": "messages after the anchor (default 10, max 25)",
            },
        },
        ["chat_id", "message_id"],
    ),
    _tool(
        "list_processes",
        "Background processes started with start_process: id, command, pid, "
        "status (running/stopped/dead — liveness re-checked against the real "
        "pid on every call, never stale), and log path.",
        {},
    ),
    _tool(
        "read_process_log",
        "Tail a background process's captured output (stdout+stderr, teed "
        "to its log file). Use after start_process to see how the server is "
        "doing; default 50 lines, max 400.",
        {
            "proc_id": {"type": "string"},
            "tail": {"type": "integer", "description": "lines from the end (default 50)"},
        },
        ["proc_id"],
    ),
    _tool(
        "await_runs",
        "WAIT for dispatched runs to settle, then get each run's outcome in "
        "one result — the collect half of batch_dispatch/dispatch_run: "
        "dispatch, await_runs, then synthesize the answers yourself. Blocks "
        "up to timeout_seconds (default 120, max 180); a run still going "
        "when time runs out is reported with its live state, honestly — "
        "never a made-up result. A run at pending_approval counts as "
        "settled: it is waiting on the user, not on time. settled=true does "
        "NOT mean succeeded — check each run's state. A run that failed, "
        "crashed or timed out carries its own guidance saying what blocked "
        "it and what to do next; act on that, never report it as done.",
        {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
                "description": "the run ids to wait for (from dispatch_run/batch_dispatch)",
            },
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 180},
        },
        ["task_ids"],
    ),
    _tool(
        "search_web",
        "Search the public web (keyless) and get title/url/host/snippet rows. "
        "Use it to DISCOVER source hosts when the user asks for research without "
        "naming sources, then propose start_research with the discovered hosts as "
        "source_allowlist — the confirmation card is where the user approves that "
        "exact egress list. Searching itself is read-only and never widens any "
        "run's network.",
        {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        ["query"],
    ),
    _tool(
        "describe_tools",
        "Full description and parameter schema for up to 8 named tools from "
        "the tool index in the system prompt. The described tools stay fully "
        "advertised for the rest of this chat. Use it when an index line is "
        "not enough to shape the call — indexed tools can also be called "
        "directly by name without describing them first.",
        {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "tool names exactly as they appear in the index",
            }
        },
        ["names"],
    ),
]

MUTATING_TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "register_mcp_server",
        "PROPOSE registering an MCP server (requires user confirmation). "
        "stdio servers need a command argv; http servers need a url (an MCP "
        "Streamable HTTP endpoint, usually ending in /mcp; legacy /sse-style "
        "HTTP+SSE servers are not supported). "
        "scope='email' binds a mail server: its read-shaped tools decide as "
        "email/read (flow freely), everything else as email/send (cards). "
        "scope='browse' binds a browser server (e.g. command "
        "['npx','@playwright/mcp@latest']): page-state reads like snapshot/"
        "screenshot/console flow freely; navigation, clicks, typing, and JS "
        "card until the user allows them (allow_mcp_tool).",
        {
            "server_id": {"type": "string"},
            "transport": {"type": "string", "description": "stdio or http"},
            "command": {"type": "array", "items": {"type": "string"}},
            "url": {"type": "string"},
            "scope": {
                "type": "string",
                "enum": ["mcp", "email", "browse"],
                "description": "policy scope for this server's tools (default mcp)",
            },
        },
        ["server_id", "transport"],
    ),
    _tool(
        "read_url",
        "PROPOSE reading ONE public web page as text (requires user confirmation "
        "— the card shows the exact URL; nothing is fetched until the user "
        "confirms). EXCEPTION: a domain the user granted with allow_fetch_domain "
        "reads inside the turn with no card (a cross-domain redirect still fails "
        "closed) AND returns a 4x larger excerpt. Use after search_web when one "
        "page would answer directly; the reply is a bounded markdown excerpt "
        "(headings/links/lists kept; pass mode='text' for flat text) and a cut "
        "is marked, never silent. On an ungranted domain every call is one card "
        "— NEVER chain read_url over several pages (that is a wall of cards): "
        "for anything multi-page or multi-source, propose start_research "
        "instead; for a domain the user reads daily, offer allow_fetch_domain "
        "once.",
        {
            "url": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": ["markdown", "text"],
                "description": "markdown (default) keeps structure; text is flat",
            },
        },
        ["url"],
    ),
    _tool(
        "run_shell",
        "PROPOSE running ONE shell command on the supervisor host with the "
        "operator's own standing (requires user confirmation — the card "
        "shows the exact command; a standing shell allow rule runs it "
        "in-turn instead). For one-off diagnostics: 'what's on port 8765?', "
        "'df -h', 'systemd status'. NOT for repo work — a cwd inside a "
        "registered repo REFUSES (a shell there edits files with no patch "
        "card; use quick_edit or dispatch_run) — and git "
        "push/pull/fetch/commit/checkout and sudo never run from chat and "
        "can never be granted. Output captured and bounded; 60s cap. For "
        "something that must keep running, propose start_process instead.",
        {
            "command": {"type": "string", "description": "the exact command, verbatim"},
            "cwd": {"type": "string", "description": "working directory (default: serve's)"},
            "timeout": {"type": "integer", "description": "seconds, capped at 60"},
        },
        ["command"],
    ),
    _tool(
        "setup_browser",
        "PROPOSE registering the built-in browser (requires user "
        "confirmation, once): the Playwright MCP server "
        "(npx @playwright/mcp@latest) under the 'browse' policy scope. "
        "After setup, page-STATE reads (snapshot, console, images) flow "
        "free; every ACTING tool (navigate, click, type, press, JS) cards "
        "until the user grants it by name with allow_mcp_tool — teach that "
        "ramp when the user starts browsing. Needs npx on the host; the "
        "result says if the handshake failed and why.",
        {},
    ),
    _tool(
        "quick_edit",
        "PROPOSE a small, single-file change as a governed micro-run "
        "(requires user confirmation unless the project auto-dispatches). "
        "The chat shortcut for 'fix the typo in README' / 'bump that "
        "version string': one coding worker, scoped to ONE named file, "
        "with a verification-first brief — and the result lands as a patch "
        "through the normal approval like every run (you never edit files "
        "yourself). For changes touching more than one file, use "
        "dispatch_run with full instructions instead.",
        {
            "repo": {"type": "string", "description": "registered repo slug or path"},
            "file": {"type": "string", "description": "the ONE file to change (repo-relative)"},
            "instruction": {"type": "string", "description": "the change, stated plainly"},
        },
        ["repo", "file", "instruction"],
    ),
    _tool(
        "start_process",
        "PROPOSE starting a LONG-LIVED background process on the supervisor "
        "host (requires user confirmation; a standing shell 'run_background' "
        "rule runs it in-turn — a plain run_shell grant deliberately does "
        "NOT cover daemons). For 'start the dev server and keep it "
        "running': output tees to a log (read_process_log), liveness is "
        "tracked honestly, stop_process ends it. Repo checkouts refuse — "
        "daemons do not run where files land as patches. Same hard guards "
        "as run_shell: git remotes/commits and sudo never run.",
        {
            "command": {"type": "string", "description": "the exact command, verbatim"},
            "cwd": {"type": "string", "description": "working directory (non-repo)"},
        },
        ["command"],
    ),
    _tool(
        "stop_process",
        "PROPOSE stopping a background process started with start_process "
        "(requires user confirmation — the user may be mid-debug on that "
        "server; a standing 'run_background' rule covering the process's "
        "command auto-stops instead, managing what it was trusted to "
        "start). SIGTERM to the process group; the row records the stop.",
        {"proc_id": {"type": "string"}},
        ["proc_id"],
    ),
    _tool(
        "allow_fetch_domain",
        "PROPOSE a standing grant to read pages from ONE exact host without a "
        "per-URL card (requires user confirmation). After the grant, read_url "
        "on that host runs inside the turn, audited. Exact host only — "
        "docs.example.com and example.com are separate grants (the run-egress "
        "matcher), and a redirect leaving the granted host fails closed. This "
        "grants ONLY Queen-side GET-as-text: no POSTs, no worker egress, no "
        "run network widening — and an explicit policy deny always wins. "
        "Offer it when the user keeps confirming reads of the same host.",
        {"domain": {"type": "string", "description": "bare domain, e.g. 'docs.python.org'"}},
        ["domain"],
    ),
    _tool(
        "revoke_policy_rule",
        "PROPOSE revoking ONE learned policy rule by id (requires user "
        "confirmation). Removes the standing grant — allow-always or "
        "session — so the next matching action cards again instead of "
        "auto-running; this NARROWS policy, it can never widen it. Rule ids "
        "are deterministic and returned by the grant that made them (e.g. "
        "'network:fetch:docs.python.org', 'shell:run:uv run pytest'); a "
        "wrong id refuses listing the known rules. Offer it when the user "
        "asks why something ran without asking, or wants a grant undone.",
        {
            "rule_id": {
                "type": "string",
                "description": "the learned rule's id, e.g. 'network:fetch:docs.python.org'",
            }
        },
        ["rule_id"],
    ),
    _tool(
        "resume_run",
        "PROPOSE resuming a crashed, timed-out, or FAILED run (requires user "
        "confirmation). Crashed/timed-out runs continue IN PLACE from their "
        "saved checkpoint cursor (checkpoint required); failed runs get a "
        "fresh attempt in their preserved worktree — prior edits, toolchain "
        "caches and installed deps intact, so the retry skips the cold "
        "setup. When the worktree is gone (preserved trees expire after 24h) "
        "the resume honestly replays from step 0 in a fresh worktree. "
        "Prefer this over a fresh dispatch_run of the same task — that redoes "
        "the work cold; diagnose_run inspects the kept tree first. "
        "Landing rules unchanged: the resumed run still lands through its "
        "own approval.",
        {"task_id": {"type": "string", "description": "the run's task id"}},
        ["task_id"],
    ),
    _tool(
        "diagnose_run",
        "PROPOSE running ONE bounded shell command inside a kept run worktree "
        "(requires user confirmation — always cards, never auto-allowed). Use "
        "it to diagnose a failed or unconfirmed run: re-run the failing test, "
        "cat a log, inspect state — in the run's own preserved evidence. The "
        "command executes sandboxed (no network, writes confined to that "
        "worktree) and its output returns here, capped at 10k chars. Only "
        "failed/unconfirmed runs keep their worktree, and only for 24h; when "
        "the tree is gone, read the audit trail via get_run instead. This "
        "cannot land, push, or modify anything outside the kept worktree. "
        "After diagnosis, resume_run continues the work in that same tree — "
        "no fresh dispatch_run needed.",
        {
            "task_id": {"type": "string", "description": "the run whose kept worktree to inspect"},
            "command": {"type": "string", "description": "the shell command to run in it"},
            "timeout_seconds": {
                "type": "integer",
                "description": "wall-clock bound (default 120, max 600)",
            },
        },
        ["task_id", "command"],
    ),
    _tool(
        "batch_dispatch",
        "PROPOSE dispatching up to 3 worker runs IN PARALLEL — one card, one "
        "confirm for the whole batch (auto-resolves only when EVERY task "
        "matches its project's auto-dispatch policy). Each task is an "
        "independent, fully governed run: own worktree, own policy, own "
        "audit trail, own re-verification. Returns one task id per task; "
        "watch them with list_runs/get_run. Set a per-task 'engine' to run the "
        "SAME brief on different coding agents (see compare-coding-engines); "
        "any explicit engine makes the whole batch card.",
        {
            "tasks": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "instructions": {"type": "string"},
                        "caste": {"type": "string", "enum": caste_names()},
                        "execution_mode": {"type": "string", "enum": ["workspace", "sandbox"]},
                        "engine": {
                            "type": "string",
                            "description": (
                                "coding agent for THIS task (builtin, claude_code, "
                                "codex, aider, pi); a CLI engine needs the project "
                                "to pin verify_command or the run fails closed"
                            ),
                        },
                    },
                    "required": ["repo", "instructions"],
                },
            }
        },
        ["tasks"],
    ),
    _tool(
        "delegate_analysis",
        "PROPOSE spawning 1-3 reasoning-only analysts (requires user "
        "confirmation). Each analyst is ONE fresh LLM conversation holding "
        "the READ tools only — no worktree, no sandbox, no file writes, "
        "nothing to land. Use for 'compare these approaches', 'read these "
        "runs and summarize', 'assess this from N angles' — analysis a "
        "worker dispatch would waste a worktree on. Each analyst's full "
        "transcript is saved as its own chat (search_chats finds it); the "
        "answers return here for YOU to synthesize. Anything that must "
        "modify files is dispatch_run, not this. Cap 3 (ADR 0041).",
        {
            "tasks": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string"},
                "description": "1-3 independent analysis prompts, each self-contained",
            },
            "context": {
                "type": "string",
                "description": "shared context prepended to every analyst's task",
            },
        },
        ["tasks"],
    ),
    _tool(
        "create_skill",
        "PROPOSE saving a new skill/template (requires user confirmation — "
        "the card shows the full instructions). Chat-authored skills carry "
        "NO capability grants (no network, no shell allowlist): they are "
        "procedural knowledge only, saved with provenance='chat'. Grants "
        "still require the CLI import paths. No silent overwrite. "
        "If the recipe's deliverable depends on data fetched during the run "
        "(fetch then summarize), its instructions MUST say to dispatch with "
        "protocol='react' — plan-mode workers fabricate those.",
        {
            "name": {"type": "string"},
            "instructions": {"type": "string", "description": "the full recipe text"},
            "description": {"type": "string"},
        },
        ["name", "instructions"],
    ),
    _tool(
        "patch_skill",
        "PROPOSE an exact find-replace in one skill/template's instructions "
        "(requires user confirmation — the card shows both strings verbatim). "
        "The skill's grants, caste, and params are untouched.",
        {
            "name": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        ["name", "old_string", "new_string"],
    ),
    _tool(
        "delete_skill",
        "PROPOSE deleting one skill/template from the registry (requires "
        "user confirmation — deletes destroy data). Schedules that reference "
        "it will fail at their next tick, so check list_schedules first.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "run_code",
        "Run a short python/shell script as a sandboxed script-worker run "
        "against a repo's worktree: deny-all egress, workspace-only writes, "
        "nothing ever lands. Auto-runs exactly where dispatch_run would "
        "auto-dispatch (the script envelope is strictly tighter); otherwise "
        "it cards with the code verbatim. Use for calculations, data "
        "crunching, one-off checks — stdout/stderr/exit code return as the "
        "tool result and are audited as run events. For a quick pure "
        "computation (no files, no repo, no network, under 10s) pass "
        "fast=true: same sandbox walls, no worktree, answer in-turn; hosts "
        "without a native sandbox fall back to the worker run automatically.",
        {
            "repo": {"type": "string", "description": "repo slug or path (hosts the worktree)"},
            "code": {"type": "string", "description": "the script source, verbatim"},
            "language": {"type": "string", "enum": ["python", "shell"]},
            "fast": {
                "type": "boolean",
                "description": "supervisor-side sandboxed 10s lane for pure computation",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "wall clock for the run (default 120, max 600) — "
                "raise it for slow toolchain work like npm install",
            },
        },
        ["repo", "code"],
    ),
    _tool(
        "read_file",
        "Read ONE file on the host as numbered lines. Paths inside the "
        "operator's roots (registered repos, workon'd projects, the skep home) "
        "read immediately; any other path becomes a confirmation card naming "
        "the exact resolved path. Explicit filesystem policy rules win either "
        "way — a deny refuses outright. BRANCH-AWARE: landed work lives on "
        "skep/ branches while the clone stays on its default checkout — pass "
        "ref to read a file from any branch/commit; with no ref, a miss falls "
        "back to the project's landing branch automatically (the result's "
        "ref/note fields say where the content came from).",
        {
            "path": {"type": "string", "description": "absolute or ~-relative file path"},
            "ref": {
                "type": "string",
                "description": "branch or commit to read from (e.g. skep/maintain)",
            },
            "offset": {"type": "integer", "description": "first line to read (1-based)"},
            "limit": {"type": "integer", "description": "max lines to return (default 200)"},
        },
        ["path"],
    ),
    _tool(
        "search_files",
        "Search file CONTENTS (ripgrep regex) or list matching FILE NAMES "
        "under one directory. Same governance as read_file: operator roots "
        "run immediately, other paths card, filesystem deny rules refuse.",
        {
            "pattern": {
                "type": "string",
                "description": "regex for content search, or a name/glob for target=files",
            },
            "path": {"type": "string", "description": "directory to search under"},
            "target": {"type": "string", "enum": ["content", "files"]},
            "file_glob": {
                "type": "string",
                "description": "only search files matching this glob, e.g. '*.py'",
            },
        },
        ["pattern", "path"],
    ),
    _tool(
        "sync_notes",
        "PROPOSE syncing ALL saved notes into an Obsidian vault as markdown "
        "(requires user confirmation — the card shows the exact vault path). "
        "Each note becomes <vault>/skep/<note_id>.md. A file the user edited "
        "by hand is NEVER overwritten: a changed note colliding with an "
        "edited file lands beside it as <note_id>.skep-conflict.md. Notes "
        "deleted in skep leave their vault files alone. The vault path is "
        "remembered after the first confirmed sync, so later calls can omit "
        "it.",
        {
            "vault_path": {
                "type": "string",
                "description": "absolute (or ~/...) path of the vault folder; "
                "omit to reuse the remembered vault",
            }
        },
        [],
    ),
    _tool(
        "forge_tool",
        "PROPOSE authoring a NEW MCP tool server for skep itself (requires "
        "user confirmation). On confirm, a coding worker writes ONE "
        "stdlib-only Python file in the operator's forge repo; that patch "
        "lands only through the normal human approval, and even then the "
        "tool cannot run until promote_tool passes its sandboxed trial and "
        "the user confirms activation. Use when a capability is missing and "
        "would be reused — check list_plugins and list_mcp_tools first so "
        "nothing is forged twice. Purpose becomes the worker's brief: state "
        "exactly what the tool must do.",
        {
            "name": {"type": "string", "description": "short tool name, e.g. 'word count'"},
            "purpose": {
                "type": "string",
                "description": "what the tool must do, concretely — the worker's brief",
            },
        },
        ["name", "purpose"],
    ),
    _tool(
        "promote_tool",
        "PROPOSE promoting a landed forged plugin to ACTIVE (requires user "
        "confirmation). On confirm: a sandboxed NO-network trial must list "
        "the server's tools and pass its zero-argument self_test; only then "
        "is the landed source installed and registered as a stdio MCP server "
        "whose tools call_mcp_tool can reach. A suspended plugin reactivates "
        "the same way. Fails honestly (and stays demoted) if the authoring "
        "run has not landed or the trial fails.",
        {"plugin_id": {"type": "string", "description": "from list_plugins"}},
        ["plugin_id"],
    ),
    _tool(
        "suspend_tool",
        "PROPOSE suspending an ACTIVE forged plugin (requires user "
        "confirmation): it is deregistered immediately and its tools stop "
        "being callable; promote_tool reactivates it. rollback=true instead "
        "retires the plugin PERMANENTLY from any state (terminal — forge a "
        "replacement afterwards).",
        {
            "plugin_id": {"type": "string", "description": "from list_plugins"},
            "rollback": {
                "type": "boolean",
                "description": "true = permanent retirement, not a pause",
            },
        },
        ["plugin_id"],
    ),
    _tool(
        "promote_skill_pack",
        "PROPOSE promoting a drafted skill pack (a SKILL.md skill shipping "
        "scripts; see list_skills' packs) to ACTIVE (requires user "
        "confirmation). On confirm, every shipped script must pass a "
        "syntax trial, and a pack declaring self_test also runs that "
        "command for real in a sandboxed no-network script run (v100-F5) — "
        "the result rides the card. allow_scripts grants shell commands, "
        "each shown verbatim on the card; omit to keep the grants requested "
        "at import. Also reactivates a suspended pack. Fails honestly if "
        "the trial fails.",
        {
            "pack_id": {"type": "string", "description": "from list_skills packs"},
            "allow_scripts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "shell commands to grant, e.g. "
                "'python .skep-skill/<id>/scripts/x.py'",
            },
        },
        ["pack_id"],
    ),
    _tool(
        "suspend_skill_pack",
        "PROPOSE suspending an ACTIVE skill pack (requires user "
        "confirmation): its registry skill is removed at once so nothing can "
        "dispatch it; promote_skill_pack reactivates it. rollback=true "
        "retires the pack PERMANENTLY (terminal — re-import to start over).",
        {
            "pack_id": {"type": "string", "description": "from list_skills packs"},
            "rollback": {
                "type": "boolean",
                "description": "true = permanent retirement, not a pause",
            },
        },
        ["pack_id"],
    ),
    _tool(
        "unregister_mcp_server",
        "PROPOSE removing a registered MCP server (requires user confirmation). "
        "Its tools stop being callable; learned allow rules for the scope are "
        "kept, so re-registering the same id reuses them.",
        {"server_id": {"type": "string"}},
        ["server_id"],
    ),
    _tool(
        "call_mcp_tool",
        "Call one tool on a registered MCP server. Read-shaped tools and "
        "tools covered by an allow rule run immediately; anything else "
        "becomes a confirmation card; a policy deny rule refuses outright.",
        {
            "server_id": {"type": "string"},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
        },
        ["server_id", "tool"],
    ),
    _tool(
        "allow_mcp_tool",
        "PROPOSE always-allowing one MCP tool (requires user confirmation). "
        "Writes a learned allow rule in the server's scope (mcp, or email for "
        "a mail-bound server); a rule that reaches into denied policy space "
        "is rejected with the deny's rule id.",
        {"server_id": {"type": "string"}, "tool": {"type": "string"}},
        ["server_id", "tool"],
    ),
    _tool(
        "set_policy",
        "PROPOSE a supervisor policy change (requires user confirmation). "
        "Only pass the fields you want to change. auto_approve is INERT (v81-F14): "
        "setting it changes nothing — the per-project phase ramp "
        "(set-phase maintain) is the only way to auto-apply verified patches. "
        "Never propose auto_approve as a fix "
        "for a denied capability or a failed run — approve the specific run instead.",
        {
            "auto_approve": {"type": "boolean"},
            "worker_cmd": {"type": "string"},
            "default_network": {"type": "array", "items": {"type": "string"}},
            "default_env_allowlist": {"type": "array", "items": {"type": "string"}},
            "default_execution_mode": {
                "type": "string",
                "enum": ["ask", "workspace", "sandbox"],
            },
            "trusted_workspace_roots": {"type": "array", "items": {"type": "string"}},
            "sandbox_required_for": {"type": "array", "items": {"type": "string"}},
            "ticker_interval_seconds": {"type": "integer"},
            "card_timeout_seconds": {
                "type": "integer",
                "description": "seconds before a pending confirmation card "
                "auto-DENIES (default 300; 0 disables the timeout)",
            },
            "default_wall_clock_seconds": {"type": "integer"},
            "default_max_iterations": {"type": "integer"},
            "default_max_actions": {"type": "integer"},
            "default_max_provider_calls": {"type": "integer"},
            "allowed_plugin_risks": {"type": "array", "items": {"type": "string"}},
        },
    ),
    _tool(
        "apply_policy_preset",
        "PROPOSE adding a curated command preset to the shell allowlist "
        "(requires user confirmation). Preset 'git' allows READ-ONLY git: "
        "status, diff, log. The worker can never add, commit, or push — "
        "landing (land_run) is the commit. For a single specific command, "
        "use allow_shell_command instead.",
        {"preset": {"type": "string", "enum": ["git"]}},
        ["preset"],
    ),
    _tool(
        "set_operator_policy",
        "PROPOSE adding one rule to the QUEEN's standing operator policy "
        "(requires user confirmation) — the document governing Queen-side "
        "scoped tools: read_file/search_files paths, search_web/read_url "
        "network audit, and run_shell/start_process shell lanes. NOT "
        "set_policy: run/worker policy is untouched. verdict 'allow' or "
        "'deny'; deny wins ties; an allow reaching into denied space is "
        "rejected with the deny's rule id. NOTE for shell allows: a granted "
        "command can read and modify files in its working directory without "
        "a patch card — say so when proposing one. shell actions: 'run' "
        "(run_shell one-offs), 'run_repo' (repo cwds — the file-write pen), "
        "'run_background' (start_process daemons); a grant for one never "
        "covers the others.",
        {
            "scope": {"type": "string", "enum": ["filesystem", "network", "shell"]},
            "action": {
                "type": "string",
                "description": (
                    "filesystem: read/write; network: connect/search/fetch; "
                    "shell: run/run_repo/run_background"
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "path glob, domain, or command prefix — e.g. '/tmp/**', "
                    "'docs.python.org', 'lsof -i'"
                ),
            },
            "verdict": {"type": "string", "enum": ["allow", "deny"]},
        },
        ["scope", "action", "pattern", "verdict"],
    ),
    _tool(
        "allow_shell_command",
        "PROPOSE adding ONE command prefix to the persistent shell allowlist "
        "(requires user confirmation), e.g. 'npm install'. Worker VERIFY "
        "commands (purpose 'verify': pytest, python running the tests) NEVER "
        "need this — they are always auto-allowed (shell_verify); the "
        "allowlist governs only non-verify steps, so do not propose grants "
        "for a task's verification. git push, pull, "
        "fetch, add, and commit can NEVER be allowlisted — no allowlist, grant, "
        "or override exists, so never propose one: refreshing a registered repo "
        "from its remote is refresh_repo (supervisor-side), and landing "
        "(land_run) is the only commit path. rm/sudo and other dangerous "
        "prefixes are also always rejected. GRANTING MEANS: this command can "
        "read and modify files in its working directory without a patch card "
        "— the user grants with eyes open.",
        {
            "command": {
                "type": "string",
                "description": "the command prefix to allow, e.g. 'npm install'",
            }
        },
        ["command"],
    ),
    _tool(
        "allow_env_bootstrap",
        "PROPOSE allowing the env-bootstrap pack in ONE card (requires user "
        "confirmation): uv venv; uv pip install; python3 -m venv; python3 -m "
        "pip install. For workers that must create a Python env — bare 'pip' "
        "is deliberately absent (missing on macOS). Same guards as "
        "allow_shell_command; riskier commands keep carding.",
        {},
        [],
    ),
    _tool(
        "approve_review",
        "PROPOSE approving a pending review (requires user confirmation): applies the "
        "patch, or resumes a gated run. Use this when a review already exists; to land "
        "a COMPLETED run (with or without an existing review), use land_run instead.",
        {
            "review_id": {"type": "string"},
            "note": {"type": "string"},
            "branch": {
                "type": "string",
                "description": "optional landing branch: skep/<task_id> (the default) "
                "or the project's auto_apply_branch — no other name is accepted "
                "from chat",
            },
        },
        ["review_id"],
    ),
    _tool(
        "land_run",
        "PROPOSE landing a completed run's patch (requires user confirmation). This is "
        "THE way to get finished work onto a branch — it opens the landing review if "
        "none exists and applies the patch in one gated step. `branch` may name the "
        "project's auto_apply_branch instead of the default skep/<task_id>; an "
        "existing branch gets the patch appended as a new commit. Landing IS how "
        "skep commits: the worker cannot "
        "create branches or commit, so never dispatch a run to 'commit' finished work "
        "and never suggest auto_approve — use this. Only a COMPLETED run with a "
        "patch can land: a failed run is never landable, and a run that changed "
        "nothing has no patch — do not propose landing either. Propose it ONCE "
        "per task: the result says when the patch landed, and a landed task "
        "never needs a second land_run.",
        {
            "task_id": {"type": "string"},
            "note": {"type": "string"},
            "branch": {
                "type": "string",
                "description": "optional landing branch: skep/<task_id> (the default) "
                "or the project's auto_apply_branch — no other name is accepted "
                "from chat",
            },
        },
        ["task_id"],
    ),
    _tool(
        "open_pr",
        "PROPOSE opening a GitHub PR for completed runs (requires user confirmation). "
        "Pass task_id for ONE run, or task_ids for SEVERAL related runs (same repo, "
        "same topic, earliest first) to land them as commits on one shared branch "
        "and open ONE PR, or branch (with repo) for an EXISTING local branch that "
        "already carries the commits. Lands each patch first when not landed yet "
        "(same gate as land_run), pushes the landing branch, and opens the PR "
        "against `base` on the operator's own gh credentials. Only runs that "
        "produced a PATCH can land — patch-less runs (e.g. run_code script runs) "
        "are skipped from a group with a note, and alone they have nothing to open "
        "a PR for. main itself never moves — merging is a separate "
        "operator-confirmed step (merge_pr).",
        {
            "task_id": {"type": "string", "description": "one run (one PR)"},
            "branch": {
                "type": "string",
                "description": "an existing local branch to push and PR as-is "
                "(v96-F4 — needs repo; never the default branch)",
            },
            "repo": {
                "type": "string",
                "description": "registered repo slug or host path (branch mode only)",
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "several related runs grouped into ONE PR "
                "(same repo; earliest run first)",
            },
            "title": {
                "type": "string",
                "description": "topic name for a grouped PR — becomes the PR "
                "title and the shared skep/<slug> branch name",
            },
            "base": {"type": "string", "description": "PR base branch (default main)"},
            "note": {"type": "string"},
        },
    ),
    _tool(
        "merge_pr",
        "PROPOSE merging an open GitHub PR (requires user confirmation in the web "
        "UI; messenger channels can never confirm it). The ONLY way a base branch "
        "like main advances — runs gh on the operator's own credentials. `pr` is "
        "a PR number or URL.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "pr": {"type": "string"},
            "strategy": {"type": "string", "enum": ["merge", "squash", "rebase"]},
        },
        ["repo", "pr"],
    ),
    _tool(
        "close_pr",
        "PROPOSE closing an open GitHub PR without merging (requires user "
        "confirmation). Nothing is destroyed: the branch and its commits stay, "
        "and a closed PR can be reopened on GitHub. delete_branch=true also "
        "deletes the PR's branch after closing. Runs gh on the operator's own "
        "credentials, like merge_pr. `pr` is a PR number or URL.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "pr": {"type": "string"},
            "delete_branch": {
                "type": "boolean",
                "description": "also delete the PR's branch after closing",
            },
        },
        ["repo", "pr"],
    ),
    _tool(
        "allow_command_review",
        "PROPOSE allowing a pending review and remembering it (requires user "
        "confirmation): a shell approval persists the command into allowed policy, "
        "a network.fetch/network.read approval persists the blocked host into the "
        "project's network allowlist; either way the gated run resumes.",
        {"review_id": {"type": "string"}},
        ["review_id"],
    ),
    _tool(
        "deny_review",
        "PROPOSE denying a pending review (requires user confirmation).",
        {"review_id": {"type": "string"}, "note": {"type": "string"}},
        ["review_id"],
    ),
    _tool(
        "register_repo",
        "PROPOSE cloning/registering a git repo URL under SKEP_HOME/repos "
        "(requires user confirmation). Returns the slug to pass to dispatch_run. "
        "THE way to get a remote repo cloned locally — never attempt a clone "
        "via shell commands or workers, and never ask the user to mkdir or "
        "clone by hand.",
        {
            "url": {"type": "string"},
            "name": {"type": "string", "description": "optional registered repo slug"},
        },
        ["url"],
    ),
    _tool(
        "create_branch",
        "PROPOSE creating a git branch in a registered repo (requires user "
        "confirmation): supervisor-side `git branch <name> <from_ref>`. Refuses "
        "the default branch and names that already exist (extending an existing "
        "branch is land_run's job). Workers can never create branches — this is "
        "the operator's verb.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "name": {"type": "string", "description": "new branch name (git-ref slug)"},
            "from_ref": {
                "type": "string",
                "description": "base ref (default: the repo's default branch)",
            },
        },
        ["repo", "name"],
    ),
    _tool(
        "delete_branch",
        "PROPOSE deleting a git branch in a registered repo (requires user "
        "confirmation). Safe form only: the default branch and any branch with "
        "unmerged work are always refused — skep never destroys work that has "
        "not landed. remote=true also deletes origin/<name> on the operator's "
        "own credentials.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "name": {"type": "string", "description": "local branch name to delete"},
            "remote": {"type": "boolean", "description": "also delete origin/<name>"},
        },
        ["repo", "name"],
    ),
    _tool(
        "merge_branch",
        "PROPOSE merging one local ref into another local branch (requires user "
        "confirmation): supervisor-side `git merge`. USE THIS when a branch has "
        "fallen behind (merge origin/main into it) or to consolidate several "
        "task branches into one integration branch before a single PR. Refuses "
        "to merge INTO the default branch — main moves only through open_pr + "
        "merge_pr. A conflict is ABORTED and reported with the conflicting "
        "paths; nothing is ever left half-merged. Workers can never merge, so "
        "this is the operator's verb — never ask a worker to run git merge.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "source": {
                "type": "string",
                "description": "ref to merge FROM, e.g. 'origin/main' or another branch",
            },
            "into": {
                "type": "string",
                "description": "local branch to merge INTO; never the default branch",
            },
        },
        ["repo", "source", "into"],
    ),
    _tool(
        "push_branch",
        "PROPOSE pushing a non-default branch to origin (requires user "
        "confirmation) — updates an existing PR branch after more landings. "
        "The default branch is always refused (main moves only through "
        "merge_pr); non-fast-forward pushes fail honestly, force-push stays a "
        "human decision. Operator credentials, like open_pr.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "name": {"type": "string", "description": "local branch to push"},
        },
        ["repo", "name"],
    ),
    _tool(
        "push_baseline",
        "PROPOSE creating the MISSING default branch on origin (requires user "
        "confirmation) — the empty-remote repair. A repo created empty on "
        "GitHub has no base branch, so open_pr fails until the synthesized "
        "baseline is pushed once. Only ever creates the missing remote base "
        "ref; refuses when origin already has the branch (an existing default "
        "branch moves only through merge_pr). Operator credentials.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "base": {
                "type": "string",
                "description": "base branch to create on origin (default: the repo's default)",
            },
        },
        ["repo"],
    ),
    _tool(
        "sync_fleet",
        "PROPOSE running the operator-pinned fleet sync command (requires "
        "user confirmation) — the machine-sync script pinned in the terminal "
        "via skep sync --set, typically commit+push+pull of the operator's "
        "own config/vault repos. Runs the pin VERBATIM: this tool can never "
        "choose or change the command, and it refuses while nothing is "
        "pinned. Publishes to remotes on operator credentials, like "
        "push_branch. Not a coding lane — repo tasks go through dispatch_run.",
        {},
        [],
    ),
    _tool(
        "unregister_repo",
        "PROPOSE removing a registered repo's managed clone (requires user "
        "confirmation). Deletes SKEP_HOME/repos/<slug> only — never the remote, "
        "never any other copy — and refuses while the repo has in-flight runs. "
        "Project bindings are separate; clean those with setup_project if needed.",
        {"name": {"type": "string", "description": "registered repo slug"}},
        ["name"],
    ),
    _tool(
        "refresh_repo",
        "PROPOSE refreshing a registered repo from its remote (requires user "
        "confirmation): the supervisor runs git fetch --prune and fast-forwards "
        "the default branch to origin. THE way to bring a registered repo up to "
        "date — workers can never fetch. Use when a branch or commit pushed to "
        "the remote after registration is not visible in repo_state.",
        {"repo": {"type": "string", "description": "registered repo slug or host path"}},
        ["repo"],
    ),
    _tool(
        "workon",
        "PROPOSE making a local directory a first-class skep workspace (requires "
        "user confirmation). If the directory is not a git repo, this runs git init "
        "and commits the current tree as the baseline — a git baseline is what makes "
        "skep's changes reviewable and revertible; raw filesystem mutation without one "
        "is out of scope permanently. Then it binds the directory to a trusted project "
        "(default pack trusted_local_dev, phase build, toolchain allowlist seeded) and "
        "returns the effective policy. Use this when the user asks skep to work on a "
        "plain local folder that is not registered. NOT for remote URLs — a Git URL "
        "gets register_repo; a repo already registered needs no workon.",
        {
            "path": {
                "type": "string",
                "description": "absolute path (or ~/...) to the directory",
            },
            "pack": {"type": "string", "description": "policy pack (default trusted_local_dev)"},
            "phase": {
                "type": "string",
                "enum": ["bootstrap", "build", "maintain", "publish_candidate"],
            },
        },
        ["path"],
    ),
    _tool(
        "setup_project",
        "PROPOSE creating or updating a trusted project from a first-party pack "
        "(requires user confirmation). For repos that are ALREADY registered; a "
        "plain local folder gets workon (which binds a project itself) — do not "
        "run both for the same directory.",
        {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "strategy": {
                "type": "string",
                "enum": ["public_free", "trusted_local_dev", "trusted_local_ops"],
            },
            "phase": {
                "type": "string",
                "enum": ["bootstrap", "build", "maintain", "publish_candidate"],
            },
            "repo_path": {"type": "string"},
            "repo_slug": {"type": "string"},
            "template_names": {"type": "array", "items": {"type": "string"}},
            "engine": {
                "type": "string",
                "enum": engine_names(),
                "description": "the project's coding agent — use this, not a "
                "policy_overrides blob, to pick claude_code/codex/aider/pi",
            },
            "groups": {
                "type": "array",
                "items": {"type": "string"},
                "description": "policy groups to attach at setup (see "
                "list_policy_groups; the setup result also SUGGESTS builtin "
                "groups matching the repo's toolchain — suggestions never "
                "attach silently, pass them here to attach)",
            },
            "policy_overrides": {"type": "object"},
            "seed_default_schedules": {"type": "boolean"},
        },
        ["project_id", "name", "strategy"],
    ),
    _tool(
        "copy_project_policy",
        "PROPOSE copying one project's policy overlay (network, shell allowlist, "
        "budgets, execution mode, auto-apply/dispatch knobs) onto another project "
        "(requires user confirmation). The target keeps its own name, strategy, "
        "phase, pack, and repo bindings — only the policy knobs copy. Use when the "
        "user wants a project governed like an existing one without re-answering "
        "every policy question.",
        {
            "src_project": {"type": "string", "description": "project_id to copy FROM"},
            "dst_project": {"type": "string", "description": "project_id to copy ONTO"},
        },
        ["src_project", "dst_project"],
    ),
    _tool(
        "set_policy_group",
        "PROPOSE creating or updating a named policy group (requires user "
        "confirmation). A group bundles convenience grants only — network "
        "hosts, shell prefixes, env vars, budgets, coding_engine — NEVER "
        "trust-ramp keys (auto_apply*, allow_git_mutation, trusted roots). "
        "Editing a group changes EVERY attached project on its next dispatch. "
        "To change it for ONE project only, pass fork_from=<source> (and "
        "repoint_project=<project_id>): the new name gets the source's policy "
        "plus these edits, the source stays untouched, and that one project "
        "swaps to the fork — all in this single confirmation.",
        {
            "name": {"type": "string", "description": "2-32 chars [a-z0-9-]"},
            "policy": {
                "type": "object",
                "description": 'groupable keys only, e.g. {"default_network": ["api.example.com"]}',
            },
            "fork_from": {
                "type": "string",
                "description": "copy-on-write: fork this existing group instead "
                "of editing it in place",
            },
            "repoint_project": {
                "type": "string",
                "description": "with fork_from: swap this project's attachment "
                "from the source group to the fork",
            },
        },
        ["name", "policy"],
    ),
    _tool(
        "delete_policy_group",
        "PROPOSE deleting a policy group by name (requires user confirmation). "
        "Refused while any project still attaches it (detach first) — nothing "
        "is ever stranded. Deleting an edited builtin reverts it to the "
        "builtin definition.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "attach_policy_group",
        "PROPOSE attaching a policy group to a project (requires user "
        "confirmation). From then on the group's grants compose live into the "
        "project's run policy — list keys union, and the project's own policy "
        "always beats a group's scalar. Attach order matters for scalars "
        "(last attached wins).",
        {
            "project_id": {"type": "string"},
            "name": {"type": "string", "description": "an existing policy group"},
        },
        ["project_id", "name"],
    ),
    _tool(
        "detach_policy_group",
        "PROPOSE detaching a policy group from a project (requires user "
        "confirmation). The project loses the group's grants on its next "
        "dispatch; the group itself is untouched.",
        {"project_id": {"type": "string"}, "name": {"type": "string"}},
        ["project_id", "name"],
    ),
    _tool(
        "dispatch_run",
        "Dispatch a coding/audit run on a repo. On trusted projects this may run "
        "immediately when project policy explicitly allows auto-dispatch and the "
        "request matches default policy; otherwise it becomes a confirmation card. "
        "Omit execution_mode to inherit a trusted project's default when it is clear. "
        "Check effective_policy FIRST: if the repo has no project binding, offer "
        "setup_project before dispatching — unbound repos run on raw global defaults. "
        "A local path the user has not confirmed exists gets a list_repos/repo_state "
        "check first — dispatching at a missing directory is refused, not carded. "
        "NOT for retrying a run whose worktree is kept (crashed/timed-out/"
        "failed): resume_run continues it in place, diagnose_run inspects it "
        "first — a fresh dispatch redoes the work cold; the result carries a "
        "'hint' line when this applies. "
        "If the task needs network hosts, shell commands, or capabilities the "
        "effective policy denies, say so and propose the policy change first — "
        "never dispatch into a known gate. "
        "Runs baseline from the repo's DEFAULT branch: to EXTEND work already landed "
        "on another branch, check repo_state, pass ref=<that branch>, and land the "
        "result back onto the same branch with land_run — otherwise the worker will "
        "not see the earlier work. "
        "One dispatch carries ONE step a worker can finish and verify on its own — "
        "never a mega-task — and its instructions state the acceptance check "
        "('verify by ...'): an improvised verify is where runs die. "
        # v101-F12: WHAT each caste is now comes from the registry, on the
        # caste param itself. What stays here is what the registry cannot know:
        # how to WRITE a brief for one. Only `document` needs that, and cutting
        # it would regress the small model's ability to use the caste well —
        # tool descriptions are load-bearing code (CLAUDE.md).
        "For caste 'document', state acceptance with a LITERAL 'Must include: "
        "a; b' line (prose is not checked) and name workspace files to ground "
        "on with a 'Files: path path' line — EXAMPLE instructions: \"Draft the "
        'intro.\\nMust include: install; sandbox\\nFiles: README.md". For '
        "anything that must FETCH sources, use start_research instead.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "instructions": {"type": "string"},
            "ref": {
                "type": "string",
                "description": "branch or rev to baseline from; REQUIRED when extending "
                "work that landed on a non-default branch",
            },
            "caste": {"type": "string", "enum": caste_names(), "description": _caste_guidance()},
            "engine": {
                "type": "string",
                "enum": engine_names(),
                "description": (
                    "coding agent for THIS task, overriding the project's "
                    "coding_engine policy; external engines run sandboxed and "
                    "refuse without a project-pinned verify_command"
                ),
            },
            "execution_mode": {
                "type": "string",
                "enum": ["workspace", "sandbox"],
                "description": "where to run this task; ask the user first if unsure",
            },
            "network": {
                "type": "array",
                "items": {"type": "string"},
                "description": "domain allowlist; omit for policy default, [] denies all",
            },
            "wall_clock_seconds": {"type": "integer"},
            "max_iterations": {"type": "integer"},
            "max_actions": {"type": "integer"},
            "max_provider_calls": {"type": "integer"},
            "protocol": {
                "type": "string",
                "enum": ["plan", "react"],
                "description": (
                    "'react' is REQUIRED when the deliverable depends on data "
                    "fetched DURING the run (fetch then summarize) — plan-mode "
                    "plans before the data exists and can only fabricate. Omit "
                    "for ordinary coding tasks."
                ),
            },
        },
        ["repo", "instructions"],
    ),
    _tool(
        "start_research",
        "PROPOSE a governed deep-research run (requires user confirmation unless a "
        "trusted project policy allows it). Sugar over dispatch_run with the deep-"
        "research template: it fetches ONLY the source_allowlist domains and writes "
        "a cited report. Returns the run id.",
        {
            "repo": {"type": "string", "description": "registered repo slug or host path"},
            "question": {"type": "string"},
            "source_allowlist": {
                "type": "array",
                "items": {"type": "string"},
                "description": "domains the researcher may fetch (the run's network allowlist)",
            },
            "seed_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "the exact result URLs from search_web — the researcher reads "
                    "these instead of each host's homepage"
                ),
            },
            "depth": {"type": "string", "enum": ["shallow", "standard", "deep"]},
            "output_format": {"type": "string", "enum": ["markdown", "html"]},
        },
        ["repo", "question", "source_allowlist"],
    ),
    _tool(
        "propose_schedule",
        "PROPOSE a recurring schedule (requires user confirmation). The ticker "
        "dispatches it every interval under the same tick-time trust gate as any "
        "schedule; reusing an existing name replaces that schedule. Give plain "
        "instructions, or a saved template name with params. Caste 'note' needs "
        "no repo: each tick posts the instructions text into this chat — use it "
        "for recurring reminders (the text is static, written once here). Caste "
        "'prompt' runs the instructions as a fresh Queen turn in THIS chat each "
        "tick (read-only: it can read the store and summarize — 'every morning "
        "summarize yesterday's runs' — but never fetch the web, card, or "
        "mutate). Only for genuinely RECURRING asks ('every morning', "
        "'weekly') — a one-time task is a dispatch_run, not a schedule.",
        {
            "name": {"type": "string", "description": "schedule name (reuse replaces)"},
            "repo": {
                "type": "string",
                "description": "registered repo slug or host path (omit for caste 'note')",
            },
            "every": {
                "type": "string",
                "description": "interval: 30s, 5m, 2h, 1d, or bare seconds",
            },
            "instructions": {"type": "string"},
            "template": {
                "type": "string",
                "description": "saved template name (instead of instructions)",
            },
            "params": {"type": "object", "description": "template placeholder values"},
            "caste": {
                "type": "string",
                "enum": ["coding", "audit", "note", "script", "digest", "prompt"],
                "description": (
                    "'note' posts the text each tick; 'script' runs the instructions "
                    "as a shell command on the supervisor host and posts its output; "
                    "'digest' posts a summary of pending approvals/runs/schedules "
                    "(no repo or instructions needed); 'prompt' runs the "
                    "instructions as a read-only Queen turn in this chat. None of "
                    "these dispatch a worker; all need the user's confirmation."
                ),
            },
            "once": {
                "type": "boolean",
                "description": "fire once then self-disable (a one-shot reminder)",
            },
            "start_at": {
                "type": "string",
                "description": "don't fire before this UTC time, e.g. 2026-07-16T09:00:00Z",
            },
            "chain": {
                "type": "string",
                "description": "run with the named schedule's LAST OUTPUT injected "
                "as labeled context (e.g. a digest that includes a disk-check "
                "script's result); chains are acyclic, max 3 deep",
            },
        },
        ["name", "every"],
    ),
    _tool(
        "delete_schedule",
        "PROPOSE deleting a recurring schedule by name (requires user "
        "confirmation). The ticker stops dispatching it, permanently.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "set_schedule_enabled",
        "PROPOSE enabling or disabling a schedule by name (requires user "
        "confirmation). Disabled schedules keep their definition; the ticker "
        "skips them until re-enabled.",
        {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
        ["name", "enabled"],
    ),
    _tool(
        "run_schedule_now",
        "PROPOSE running a saved schedule immediately (requires user "
        "confirmation). On confirm the schedule becomes due and the ticker "
        "dispatches it on its next tick — same policy, same delivery to its "
        "bound chat, same health tracking as a scheduled run. Needs an "
        "ENABLED schedule; its next regular run then lands one full interval "
        "after this one. Use this instead of re-running a schedule's "
        "instructions through run_code or dispatch_run.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "set_run_completion_notify",
        "PROPOSE turning the run-completed notification on or off (requires user "
        "confirmation). When on, every completed run posts one summary line into "
        "its dispatching chat and pushes it to that chat's messenger. Failure "
        "lines are always posted regardless. Default off.",
        {"enabled": {"type": "boolean"}},
        ["enabled"],
    ),
    _tool(
        "set_assistant_model",
        "PROPOSE switching the assistant LLM (requires user confirmation). "
        "scope 'default' (the default) changes the SAVED assistant config — "
        "every chat and every default worker inherits it on their next turn. "
        "scope 'chat' changes only THIS chat's model and nothing else; pass "
        "model 'default' with scope 'chat' to clear the override. Optional "
        "base_url/protocol switch providers in the same card (protocol: "
        "'ollama', 'openai-compat', 'anthropic', 'openai-responses', or "
        "'bedrock' (AWS, keys from the daemon environment) — scope 'default' "
        "only). "
        "Never carries an API key: secrets are set in Settings, never "
        "through chat.",
        {
            "model": {
                "type": "string",
                "description": "model name, e.g. 'claude-sonnet-5' or 'qwen3:32b'",
            },
            "scope": {"type": "string", "enum": ["default", "chat"]},
            "base_url": {
                "type": "string",
                "description": "provider endpoint URL (scope 'default' only)",
            },
            "protocol": {"type": "string", "enum": list(get_args(LLMProtocol))},
        },
        ["model"],
    ),
    _tool(
        "add_provider",
        "PROPOSE registering an LLM provider profile (requires user "
        "confirmation). Pass preset (a list_provider_presets id) OR "
        "protocol+base_url+model+provider_id. api_key_env is the NAME of "
        "the key's env var — never the key value. activate=true also "
        "switches the assistant.",
        {
            "preset": {"type": "string", "description": "preset_id from the catalog"},
            "provider_id": {"type": "string", "description": "short id, e.g. 'openrouter'"},
            "protocol": {"type": "string", "enum": sorted(PROVIDER_PROTOCOLS)},
            "base_url": {"type": "string", "description": "provider endpoint URL"},
            "model": {"type": "string", "description": "default model"},
            "api_key_env": {
                "type": "string",
                "description": "env var NAME holding the key (never the value)",
            },
            "cost_class": {"type": "string", "enum": sorted(PROVIDER_COST_CLASSES)},
            "activate": {"type": "boolean", "description": "also switch the assistant"},
        },
        [],
    ),
    _tool(
        "use_provider",
        "PROPOSE switching the assistant to a registered provider profile "
        "(requires user confirmation). Writes it through to the saved "
        "assistant config.",
        {"provider_id": {"type": "string"}},
        ["provider_id"],
    ),
    _tool(
        "remove_provider",
        "PROPOSE deleting a provider profile (requires user confirmation). "
        "The saved assistant config is untouched.",
        {"provider_id": {"type": "string"}},
        ["provider_id"],
    ),
    _tool(
        "set_tts_provider",
        "PROPOSE setting the voice (TTS) provider for messenger replies "
        "(requires user confirmation). 'none' (default) = voice off. "
        "'piper' = LOCAL — nothing leaves this machine. 'edge' (Microsoft) "
        "and 'openai' are CLOUD services: every spoken reply's TEXT leaves "
        "this machine when one of those is chosen. When set, Discord-bound "
        "replies also arrive as voice messages. Web-UI voice is separate "
        "(the browser toggle) and needs no provider.",
        {"provider": {"type": "string", "enum": ["none", "piper", "edge", "openai"]}},
        ["provider"],
    ),
    _tool(
        "set_skill_observer",
        "PROPOSE turning the conversation-skill observer on or off (requires "
        "user confirmation). When on, a background sweep watches completed "
        "chat turns for multi-step procedures and proposes skill DRAFTS into "
        "the review queue — heuristic only, no extra model calls, and drafts "
        "never activate without your approval. Default off.",
        {"enabled": {"type": "boolean"}},
        ["enabled"],
    ),
    _tool(
        "set_personality",
        "PROPOSE changing THIS chat's reply style (requires user confirmation). "
        "Style only — never changes tools, gates, or policy. Value: 'concise', "
        "'technical', 'friendly', 'custom:<free text>', or 'default' to reset.",
        {"value": {"type": "string"}},
        ["value"],
    ),
    _tool(
        "set_persona",
        "PROPOSE setting the profile-level PERSONA (requires user "
        "confirmation): the identity every chat starts with — name, how to "
        "address the user, tone. Identity only, capped at 2000 chars; it can "
        "never change tools, gates, or policy (the operating rules always "
        "win). Pass 'default' to clear. Per-chat style is set_personality.",
        {"text": {"type": "string", "description": "the persona text, or 'default' to clear"}},
        ["text"],
    ),
    _tool(
        "discord_delete_message",
        "PROPOSE deleting a message in a Discord channel skep's bot can see "
        "(moderation). Requires user confirmation in the WEB UI — deliberately "
        "never confirmable from Discord itself. Needs the discord channel "
        "configured in Settings.",
        {
            "channel_id": {"type": "string", "description": "Discord channel id"},
            "message_id": {"type": "string", "description": "id of the message to delete"},
        },
        ["channel_id", "message_id"],
    ),
    _tool(
        "discord_timeout_member",
        "PROPOSE timing out a Discord guild member for N minutes (moderation). "
        "Requires user confirmation in the WEB UI — deliberately never "
        "confirmable from Discord itself.",
        {
            "guild_id": {"type": "string"},
            "user_id": {"type": "string", "description": "the member to time out"},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
        },
        ["guild_id", "user_id", "minutes"],
    ),
    _tool(
        "set_task_due",
        "PROPOSE setting or changing a task due date (requires user confirmation).",
        {"task_id": {"type": "string"}, "due_at": {"type": "string"}},
        ["task_id", "due_at"],
    ),
    _tool(
        "delete_note",
        "PROPOSE deleting a note (requires user confirmation).",
        {"note_id": {"type": "string"}},
        ["note_id"],
    ),
    _tool(
        "delete_task",
        "PROPOSE deleting a task (requires user confirmation).",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _tool(
        "approve_memory_proposal",
        "PROPOSE approving a memory proposal into durable memory (requires user confirmation).",
        {"proposal_id": {"type": "string"}},
        ["proposal_id"],
    ),
    _tool(
        "reject_memory_proposal",
        "PROPOSE rejecting a memory proposal with a reason "
        "(requires user confirmation). It never becomes durable memory.",
        {"proposal_id": {"type": "string"}, "reason": {"type": "string"}},
        ["proposal_id", "reason"],
    ),
    _tool(
        "forget_memory",
        "PROPOSE forgetting (soft-deleting) a durable memory item (requires user confirmation).",
        {"memory_id": {"type": "string"}},
        ["memory_id"],
    ),
    _tool(
        "remember",
        "File something worth keeping as a MEMORY PROPOSAL — runs in-turn "
        "(the proposal is inert: NOTHING is injected into future prompts "
        "until the user approves it via approve_memory_proposal). Use when "
        "the user states a durable preference, project fact, or standing "
        "instruction ('always X', 'I prefer Y', 'this repo uses Z'). Not "
        "for chit-chat or things the transcript already keeps — memory is "
        "for what future conversations need. Tell the user you filed it.",
        {
            "content": {"type": "string", "description": "the fact, stated plainly"},
            "memory_class": {
                "type": "string",
                "enum": [
                    "durable_preference",
                    "project_fact",
                    "todo",
                    "not_to_do",
                    "reminder",
                    "policy_hint",
                ],
                "description": "what kind of memory this is (default durable_preference)",
            },
            "project": {"type": "string", "description": "project id when project-scoped"},
            "rationale": {"type": "string", "description": "why this is worth keeping"},
        },
        ["content"],
    ),
]

TOOL_SPECS = READ_TOOL_SPECS + MUTATING_TOOL_SPECS
_TOOL_DESCRIPTIONS = {t["function"]["name"]: str(t["function"]["description"]) for t in TOOL_SPECS}


def tool_description(name: str) -> str:
    """v54-F3 (ADR 0033): the spec's plain-English description, for the human.

    The confirmation card shows it so 'dispatch_run' + raw JSON args stop being
    the only explanation of what the user is approving. '' for unknown names."""
    return _TOOL_DESCRIPTIONS.get(name, "")


READ_TOOL_NAMES = {t["function"]["name"] for t in READ_TOOL_SPECS}
MUTATING_TOOL_NAMES = {t["function"]["name"] for t in MUTATING_TOOL_SPECS}

# v83-F5 (ADR 0042): read tools with NETWORK egress, refused in unattended
# (scheduled) turns. Nobody is watching a scheduled turn, so it reads the
# store only — an injected page chaining further granted fetches every
# morning is a real surface, and mutations refusing (read_only) already
# closes the acting half; this closes the fetching half.
UNATTENDED_BLOCKED_READ_TOOLS: frozenset[str] = frozenset({"search_web"})
UNATTENDED_READ_REFUSAL = (
    "does not run in a scheduled turn — nobody is watching, so scheduled "
    "turns read the store only (runs, approvals, chats, notes, memory). "
    "Do web reads in a live chat instead."
)

# -- v74-F3: progressive tool disclosure --------------------------------------
# The R4 round made every description a mini-manual; the manual is right, the
# delivery was wrong — 54KB of specs resent every round. The index (one line
# per tool, categorized) rides the prompt; the core set + this chat's
# described-active tools carry full schemas; describe_tools fetches the rest
# on demand. Advertisement only, never permission (I5/I6): the executor
# dispatches on the NAME, mutations still card, deny space stays unreachable.

DESCRIBE_TOOL_NAME = "describe_tools"
DESCRIBE_TOOLS_MAX = 8

# The ~10 highest-frequency tools (v73 field-test data) + describe_tools.
CORE_TOOL_NAMES = frozenset(
    {
        "list_runs",
        "get_run",
        "list_approvals",
        "dispatch_run",
        "effective_policy",
        "repo_state",
        "list_repos",
        "list_schedules",
        CLARIFY_TOOL_NAME,
        "search_chats",
        DESCRIBE_TOOL_NAME,
    }
)

# Every tool belongs to exactly one category; the index generator walks
# TOOL_SPECS, so a new tool always appears — a test pins that it is also
# categorized (the v25 lockstep lesson).
TOOL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "runs": (
        "list_runs",
        "get_run",
        "list_approvals",
        "await_runs",
        "dispatch_run",
        "batch_dispatch",
        "delegate_analysis",
        "quick_edit",
        "resume_run",
        "diagnose_run",
        "run_code",
        "approve_review",
        "deny_review",
        "allow_command_review",
        "set_run_completion_notify",
    ),
    "repos & landing": (
        "list_repos",
        "repo_state",
        "git_log",
        "git_diff",
        "list_worktrees",
        "list_prs",
        "register_repo",
        "unregister_repo",
        "refresh_repo",
        "create_branch",
        "delete_branch",
        "merge_branch",
        "push_branch",
        "push_baseline",
        "sync_fleet",
        "land_run",
        "open_pr",
        "merge_pr",
        "close_pr",
        "workon",
    ),
    "schedules": (
        "list_schedules",
        "propose_schedule",
        "delete_schedule",
        "set_schedule_enabled",
        "run_schedule_now",
    ),
    "policy & projects": (
        "get_policy",
        "effective_policy",
        "set_policy",
        "apply_policy_preset",
        "set_operator_policy",
        "allow_shell_command",
        "allow_env_bootstrap",
        "setup_project",
        "copy_project_policy",
        "list_projects",
        # v97-F3 (ADR 0048): reusable convenience-grant bundles.
        "list_policy_groups",
        "set_policy_group",
        "delete_policy_group",
        "attach_policy_group",
        "detach_policy_group",
        # v109-F9: the narrowing half of the learned-grant verbs.
        "revoke_policy_rule",
    ),
    "chats & memory": (
        "search_chats",
        "list_chats",
        "get_chat_messages",
        "get_chat_context",
        "list_memory",
        "search_memory",
        "list_memory_proposals",
        "approve_memory_proposal",
        "reject_memory_proposal",
        "forget_memory",
        "remember",
        CLARIFY_TOOL_NAME,
        DESCRIBE_TOOL_NAME,
    ),
    "notes & tasks": (
        "list_notes",
        "list_tasks",
        "add_note",
        "add_task",
        "complete_task",
        "reopen_task",
        "sync_notes",
        "set_task_due",
        "delete_note",
        "delete_task",
    ),
    "skills": (
        "list_templates",
        "list_skills",
        "view_skill",
        "create_skill",
        "patch_skill",
        "delete_skill",
        "set_skill_observer",
        "promote_skill_pack",
        "suspend_skill_pack",
    ),
    "research & web": (
        "search_web",
        "start_research",
        "read_url",
        "allow_fetch_domain",
    ),
    "mcp & plugins": (
        "list_mcp_servers",
        "list_mcp_tools",
        "register_mcp_server",
        "unregister_mcp_server",
        "call_mcp_tool",
        "allow_mcp_tool",
        "forge_tool",
        "promote_tool",
        "suspend_tool",
        "list_plugins",
        "setup_browser",
    ),
    "files": ("read_file", "search_files", "run_shell"),
    "processes": ("start_process", "stop_process", "list_processes", "read_process_log"),
    "assistant": (
        "set_assistant_model",
        "list_providers",
        "list_provider_presets",
        "add_provider",
        "use_provider",
        "remove_provider",
        "set_personality",
        "set_persona",
        "set_tts_provider",
    ),
    "discord": ("discord_delete_message", "discord_timeout_member"),
}
_TOOL_CATEGORY_BY_NAME = {
    name: category for category, names in TOOL_CATEGORIES.items() for name in names
}
_SPEC_BY_NAME = {t["function"]["name"]: t for t in TOOL_SPECS}

_TOOL_INDEX_HEADER = (
    "Tool index — every tool you can call, by category, as name(args). "
    "'?'=optional arg, '…'=more args (describe_tools has the rest), "
    "'*'=goes through the confirmation path: it shows the user a card UNLESS "
    "project policy auto-allows it. No gloss after a tool means its name says "
    "what it does; a tool listed name-only already carries its full schema in "
    "this request. Call any tool directly by name; call "
    f"describe_tools(names=[...]) for up to {DESCRIBE_TOOLS_MAX} full schemas "
    "when an index line is not enough:"
)
# v99-F1: the index LOCATES a tool; describe_tools is the manual. Budgets, not
# prose — every rule below is mechanical, so no per-tool curation can drift.
_INDEX_SUMMARY_CHARS = 44
_INDEX_ARG_CHARS = 6
# Boilerplate the '*' legend states once. 67 descriptions opened with PROPOSE
# and 64 carried a "(requires …confirmation…)" clause — the old 80-char cap
# then truncated mid-boilerplate, spending the budget to say nothing.
_INDEX_BOILERPLATE = re.compile(r"^PROPOSE\s+|\s*\(requires[^)]*\)\s*", re.IGNORECASE)
# Stopwords for the redundancy prune. A gloss whose content words are already
# in the name+args is telling the model what it just read.
_INDEX_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "this",
        "that",
        "its",
        "it",
        "and",
        "or",
        "with",
        "as",
        "one",
        "you",
        "your",
        "is",
        "are",
        "be",
        "from",
        "by",
        "at",
        "into",
        "when",
        "what",
        "new",
        "up",
    }
)


def _index_tokens(text: str) -> set[str]:
    return {
        word.lower()[:5]
        for word in re.findall(r"[A-Za-z]+", text)
        if len(word) > 2 and word.lower() not in _INDEX_STOPWORDS
    }


def _index_args(spec: dict[str, Any]) -> str:
    function = spec["function"]
    params = list(function["parameters"].get("properties") or {})
    required = set(function["parameters"].get("required") or ())
    shown = [name if name in required else f"{name}?" for name in params[:_INDEX_ARG_CHARS]]
    return ", ".join(shown) + ("…" if len(params) > _INDEX_ARG_CHARS else "")


def _index_gloss(spec: dict[str, Any], args: str) -> str:
    summary = _INDEX_BOILERPLATE.sub(
        "", str(spec["function"]["description"]).split(". ")[0].rstrip(".")
    ).strip()
    if not summary:
        return ""
    name_tokens = _index_tokens(spec["function"]["name"]) | _index_tokens(args)
    if not _index_tokens(summary) - name_tokens:
        return ""  # says nothing name(args) did not
    if len(summary) > _INDEX_SUMMARY_CHARS:
        summary = summary[:_INDEX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return summary


def _index_line(spec: dict[str, Any]) -> str:
    name = spec["function"]["name"]
    # The marker is derived from MUTATING_TOOL_NAMES, never from the prose:
    # six tools in that set never said "PROPOSE", so the old index
    # under-reported the confirmation path (I8).
    mark = "*" if name in MUTATING_TOOL_NAMES else ""
    if name in CORE_TOOL_NAMES:
        return f"{name}{mark}"  # its full schema is in this same request
    args = _index_args(spec)
    gloss = _index_gloss(spec, args)
    return f"{name}{mark}({args})" + (f" — {gloss}" if gloss else "")


def _tool_index_block() -> str:
    grouped: dict[str, list[str]] = {category: [] for category in TOOL_CATEGORIES}
    for spec in TOOL_SPECS:
        category = _TOOL_CATEGORY_BY_NAME.get(spec["function"]["name"], "other")
        grouped.setdefault(category, []).append(_index_line(spec))
    lines = [_TOOL_INDEX_HEADER]
    for category, entries in grouped.items():
        if not entries:
            continue
        lines.append(f"[{category}]")
        lines.extend(f"- {entry}" for entry in entries)
    return "\n".join(lines)


# Generated from the registry at import time — the index CANNOT drift.
TOOL_INDEX_BLOCK = _tool_index_block()


def advertised_tool_specs(
    active: list[str] | tuple[str, ...], *, read_only: bool = False
) -> list[dict[str, Any]]:
    """The specs an indexed-delivery round advertises: core + this chat's
    described-active tools, in registry order. read_only turns (v67 /btw)
    keep only the read-shaped part."""
    names = CORE_TOOL_NAMES | set(active)
    source = READ_TOOL_SPECS if read_only else TOOL_SPECS
    return [t for t in source if t["function"]["name"] in names]


def _run_view(record: Any, *, store: RunStore | None = None) -> dict[str, Any]:
    view = {
        "task_id": record.task_id,
        "state": record.state,
        "repo": record.repo,
        "summary": record.summary,
        "verification": record.verification_outcome,
        "updated_at": record.updated_at,
        # v79-F2: the backward resume pointer rides every run view.
        "resume_of": record.resume_of,
    }
    if store is not None:
        # v79-F2: and the forward one — an approved gate resumes under a NEW
        # task_id; both directions visible means the Queen follows the chain
        # instead of guessing (field test 2026-07-21).
        view["resumed_as"] = store.resumed_as_for(record.task_id)
        view.update(actions.created_transition_views_for_task(store, record.task_id))
        # v20-F3: carry the re-verification signal onto the chat run list too.
        view["reverification"] = actions.reverification_summary(
            store.reverification_for(record.task_id)
        )
        # v59-F1: landing state rides the list. The Queen polls list_runs, so a
        # completed-but-unlanded patch must be visible here, not only in get_run.
        applied = actions.applied_branch_for(store, record.task_id)
        view["applied_branch"] = applied
        view["unlanded_patch"] = (
            record.state == "completed"
            and applied is None
            and actions.patch_path(store, record.task_id) is not None
        )
    return view


def _schedule_summary(schedule: Any) -> dict[str, Any]:
    """v73-F7: the compact row every list_schedules item carries."""
    return {
        "name": schedule.name,
        "caste": schedule.worker_kind,
        "every_seconds": schedule.interval_seconds,
        "enabled": schedule.enabled,
        "last_run_at": schedule.last_run_at,
        "last_task_id": schedule.last_task_id,
        "last_state": schedule.last_state,
        "next_run_at": schedule.next_run_at,
        "chat_bound": schedule.chat_id is not None,
    }


def fetch_grant_decision(store: RunStore, host: str) -> AutonomyDecision | None:
    """v72-F7: the granted-domain read_url lane. An explicit ``network/fetch``
    rule decides — allow runs in-turn, deny refuses without a card; no
    matching rule returns None and the per-URL card stays exactly as it was.
    Domain matching is ``domain_allowed`` (the run-egress matcher): EXACT
    host only — a grant on example.com does NOT cover docs.example.com;
    each host is its own operator decision (fail closed)."""
    from ..policy_schema import (
        DEFAULT_DENY_RULE_ID,
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        decide,
        document_from_settings,
        resolve,
    )

    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = document_from_settings(raw) or PolicyDocument()
    decision = decide(resolve(document), "network", "fetch", host, template=document.template)
    if decision.rule_id == DEFAULT_DENY_RULE_ID:
        return None
    if decision.verdict == "allow":
        return AutonomyDecision(
            verdict="allow",
            reason="network.allow.fetch_grant",
            detail=host,
            decided_by=decision.decided_by,
        )
    if decision.verdict == "deny":
        return AutonomyDecision(
            verdict="deny",
            reason="network.deny.fetch_rule",
            detail=f"an explicit deny covers {host!r}",
            decided_by=decision.decided_by,
        )
    return None  # require_approval → the ordinary per-URL card


# v83-F9: one-off Queen-side shell — bounds.
RUN_SHELL_TIMEOUT_CAP = 60
RUN_SHELL_OUTPUT_CAP = 10_000


def _cwd_registered_repo(store: RunStore, holder: ConfigHolder, cwd: str | None) -> str | None:
    """The registered repo (clone or workon binding) containing ``cwd``, if
    any — the v81-F10 ``known_repos`` resolver, so every surface agrees on
    what counts as a repo."""
    from pathlib import Path

    from .registry import known_repos

    resolved = (Path(cwd).expanduser() if cwd else Path.cwd()).resolve()
    for item in known_repos(repos_root(holder), store):
        repo_path = Path(str(item["path"])).resolve()
        if resolved == repo_path or repo_path in resolved.parents:
            return str(item["name"])
    return None


def queen_shell_decision(
    store: RunStore,
    holder: ConfigHolder,
    *,
    command: str,
    cwd: str | None,
    background: bool = False,
) -> AutonomyDecision | None:
    """v83-F9: the run_shell lane. Hard guards first (git/sudo — deny,
    ungrantable); a repo cwd needs an explicit ``shell/run_repo`` allow and
    otherwise REFUSES (a shell in a checkout is a file-write pen with no
    patch card — review item 2); elsewhere ``shell/run``: allow → in-turn,
    explicit deny → refuse, default → the per-command card.

    v83-F8: ``background=True`` (start_process) decides on the
    ``run_background`` action instead — a daemon is a different promise
    than a 60s one-off, so a 'run' grant never covers it (review item 3);
    repo cwds refuse flat (daemons do not belong in checkouts)."""
    from ..policy_resolver import resolve_operator_policy
    from ..policy_schema import DEFAULT_DENY_RULE_ID
    from ..shell_prefixes import queen_command_line_refusal

    if not command.strip():
        return None
    # v109-F1: judged per segment — `cd <repo> && git checkout <branch>` ran
    # from chat because argv[0] was `cd`. Malformed lines still fall to the
    # card so the honest error surfaces on confirm.
    reason = queen_command_line_refusal(command)
    if reason is not None:
        return AutonomyDecision(
            verdict="deny",
            reason="shell.deny.queen_guard",
            detail=reason,
            decided_by="queen-shell-guard",
        )
    policy = resolve_operator_policy(store)
    repo = _cwd_registered_repo(store, holder, cwd)
    if repo is not None:
        if background:
            return AutonomyDecision(
                verdict="deny",
                reason="shell.deny.repo_cwd_background",
                detail=(
                    f"the working directory is inside registered repo {repo!r} — "
                    "daemons do not run in checkouts (a background process there "
                    "could modify files with no patch card, forever). Run it from "
                    "a non-repo directory, or use dispatch_run for repo work."
                ),
                decided_by="queen-shell-guard",
            )
        repo_decision = policy.decision("shell", "run_repo", command)
        if repo_decision.verdict == "allow":
            return AutonomyDecision(
                verdict="allow",
                reason="shell.allow.run_repo_rule",
                detail=command,
                decided_by=repo_decision.decided_by,
            )
        return AutonomyDecision(
            verdict="deny",
            reason="shell.deny.repo_cwd",
            detail=(
                f"the working directory is inside registered repo {repo!r} — a "
                "shell command there can modify files with no patch card, so it "
                "refuses by default. For a governed one-file change use "
                "quick_edit (or dispatch_run for more); to open repo shells "
                "deliberately, set_operator_policy scope='shell' "
                "action='run_repo' with the command prefix."
            ),
            decided_by="queen-shell-guard",
        )
    run_decision = policy.decision("shell", "run_background" if background else "run", command)
    if run_decision.verdict == "allow":
        return AutonomyDecision(
            verdict="allow",
            reason="shell.allow.run_rule",
            detail=command,
            decided_by=run_decision.decided_by,
        )
    if run_decision.verdict == "deny" and run_decision.rule_id != DEFAULT_DENY_RULE_ID:
        return AutonomyDecision(
            verdict="deny",
            reason="shell.deny.rule",
            detail="an explicit operator deny covers this command",
            decided_by=run_decision.decided_by,
        )
    return None  # default → the ordinary per-command card


def _draft_excerpt(artifacts: list[dict[str, Any]]) -> str | None:
    """v72-F2: a document run's deliverable, quotable without a second hop.
    Reads the audit copy of ``draft.md`` (capped); None for other runs."""
    row = next(
        (
            a
            for a in artifacts
            if a.get("kind") == "file" and str(a.get("path") or "").endswith("draft.md")
        ),
        None,
    )
    if row is None:
        return None
    try:
        text = Path(str(row["path"])).read_text(encoding="utf-8")
    except OSError:
        return None
    return text[:SCRIPT_RUN_OUTPUT_CAP]


def _get_run_guidance(
    run: dict[str, Any],
    reverify: Any = None,
    *,
    has_patch: bool = False,
    applied_branch: str | None = None,
) -> str | None:
    if run.get("state") in FAILED_RUN_STATES:
        return (
            "This run failed. Tell the user the state, summary, and verification_details. "
            "Do not say it is still running. Explain the policy or blocker that stopped it, "
            "including any policy_blocks or other blockage visible in transitions, approvals, "
            "commands, or artifacts. Offer policy-compliant next steps, such as retrying "
            "with corrected "
            "instructions, requesting the needed approval, changing the policy through a "
            "confirmed set_policy action, or using a workaround that stays within current "
            "policy. Never suggest overriding policy or bypassing approvals."
        )
    # v56-F5 (ADR 0038): a run waiting on the operator says so — a polling
    # Queen must never show a pending gate without naming the next step.
    if run.get("state") == "pending_approval":
        return (
            "This run is WAITING ON THE OPERATOR: an approval gate is pending. Tell the "
            "user plainly what is waiting (the approvals on this run carry the reason and "
            "review_id) and how to resolve it — approve or deny in the Approvals view or "
            "with /approve <review_id>. Do not describe the run as still working, and "
            "never suggest bypassing the gate."
        )
    # v20-F3: a completed run the supervisor could not re-verify must never be
    # relayed as verified/passed just because the worker claimed so.
    if run.get("state") == "completed" and reverify is not None and not reverify.confirmed:
        return (
            "The supervisor could NOT confirm this run's verification "
            f"(re-verification outcome: {reverify.outcome}). Tell the user this explicitly. "
            "Do not present the run as verified or passed, and do not suggest landing it "
            "without a human reviewing the patch first."
        )
    # v22-F4: a completed, confirmed run with an unlanded patch — the next step
    # is ALWAYS the landing approval, never another run.
    if run.get("state") == "completed" and has_patch and applied_branch is None:
        return (
            "This run completed and its patch is ready to land. The next step is the "
            "land_run action — landing IS how skep commits. If the user asked for the "
            "work on the project's auto_apply_branch, pass branch=<it>. Never dispatch "
            "another run to 'stage', 'commit', or 'branch' finished work."
        )
    return None


def execute_read_tool(
    name: str, args: dict[str, Any], *, store: RunStore, holder: ConfigHolder
) -> Any:
    if name == DESCRIBE_TOOL_NAME:
        # v74-F3: on-demand schema fetch. The chat engine persists the
        # described names as the chat's active tools (it holds the chat_id);
        # this handler only reads the registry.
        requested = [str(n).strip() for n in (args.get("names") or []) if str(n).strip()]
        if not requested:
            return {
                "error": (
                    f"pass names: a list of up to {DESCRIBE_TOOLS_MAX} tool "
                    "names from the tool index in the system prompt"
                )
            }
        described: dict[str, Any] = {}
        if len(requested) > DESCRIBE_TOOLS_MAX:
            described["note"] = (
                f"only the first {DESCRIBE_TOOLS_MAX} of {len(requested)} names described"
            )
            requested = requested[:DESCRIBE_TOOLS_MAX]
        described["tools"] = [
            dict(_SPEC_BY_NAME[n]["function"]) for n in requested if n in _SPEC_BY_NAME
        ]
        unknown = [n for n in requested if n not in _SPEC_BY_NAME]
        if unknown:
            described["unknown"] = {
                "names": unknown,
                "note": (
                    "no tools by these names — the tool index in the system "
                    "prompt lists everything that exists"
                ),
            }
        return described
    if name == "list_plugins":
        from ..forge import load_plugins
        from ..mcp_client import load_mcp_servers

        servers = load_mcp_servers(store)
        return {
            "plugins": [
                {
                    "plugin_id": record.plugin_id,
                    "name": record.name,
                    "purpose": record.purpose,
                    "state": record.state,
                    "task_id": record.task_id,
                    "landed_branch": actions.applied_branch_for(store, record.task_id),
                    "server_id": record.server_id,
                    "registered": record.server_id in servers,
                    "trial": record.trial,
                }
                for record in load_plugins(store).values()
            ]
        }
    if name == "list_mcp_servers":
        from ..mcp_client import load_mcp_servers

        return {
            "servers": [
                {
                    "server_id": config.server_id,
                    "transport": config.transport,
                    "command": list(config.command),
                    "url": config.url,
                    "scope": config.scope,
                }
                for config in load_mcp_servers(store).values()
            ]
        }
    if name == "list_mcp_tools":
        from ..mcp_client import (
            MCPClient,
            MCPError,
            classify_mcp_risk,
            load_mcp_servers,
            mcp_scope_decision,
            runner_for_config,
        )

        server_id = str(args["server_id"])
        config = load_mcp_servers(store).get(server_id)
        if config is None:
            return {"error": f"no MCP server {server_id!r}; register_mcp_server first"}
        try:
            tools = MCPClient(config, runner=runner_for_config(config)).list_tools()
        except MCPError as exc:
            return {"error": str(exc)}
        from ..mcp_client import classify_browse_action

        views = []
        for tool in tools:
            decision = mcp_scope_decision(store, tool)
            views.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    # v71-F2: a browse-bound tool's honest label is its browse
                    # action — the generic ladder would misread browser_* names.
                    "risk": (
                        classify_browse_action(tool.name)
                        if config.scope == "browse"
                        else classify_mcp_risk(tool)
                    ),
                    "policy": decision.verdict,
                    "decided_by": decision.decided_by,
                }
            )
        return {"server_id": server_id, "tools": views}
    if name == "await_runs":
        # v71-F3: the collect half of dispatch — the _script_run_result wait
        # pattern generalized over N runs. No new execution model (ADR 0025
        # stands): workers stay blind to each other; the synthesis happens in
        # the Queen's next round, over these results.
        import time

        raw_ids = args.get("task_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"error": "task_ids must be a non-empty array of run ids"}
        if len(raw_ids) > 5:
            return {"error": "await_runs caps at 5 runs per call — split the wait"}
        task_ids = [str(task_id) for task_id in raw_ids]
        unknown = [task_id for task_id in task_ids if store.get_run(task_id) is None]
        if unknown:
            return {
                "error": f"no runs named {unknown} — list_runs shows recent ids; "
                "await_runs only waits on runs that exist"
            }
        from ..cli_cmds import STATE_EXIT_CODES

        timeout = min(int(args.get("timeout_seconds") or 120), 180)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {
                task_id: str(store.get_run(task_id).state)  # type: ignore[union-attr]
                for task_id in task_ids
            }
            if all(state in STATE_EXIT_CODES for state in states.values()):
                break
            time.sleep(_SCRIPT_RUN_POLL_SECONDS)
        views = []
        still_running: list[str] = []
        settled_guidance: list[str] = []
        for task_id in task_ids:
            record = store.get_run(task_id)
            if record is None:  # existed at entry; runs are never deleted
                continue
            view = _run_view(record, store=store)
            view["settled"] = str(record.state) in STATE_EXIT_CODES
            if not view["settled"]:
                still_running.append(task_id)
                views.append(view)
                continue
            # v88-F3: a settled run gets the SAME coaching get_run gives. This
            # is the tool the Queen BLOCKS in, and a terminal failure used to
            # arrive here as a bare settled=true with no guidance at all — which
            # reads as success, so the turn ended silently and nothing was
            # retried. The failed-run script already existed; it was only wired
            # into get_run (I8: the record tells the truth; I9: errors teach).
            view["verification_details"] = record.verification_details
            guidance = _get_run_guidance(
                view,
                store.reverification_for(task_id),
                has_patch=actions.patch_path(store, task_id) is not None,
                applied_branch=view.get("applied_branch"),
            )
            if guidance is not None:
                view["guidance"] = guidance
                settled_guidance.append(f"{task_id}: {guidance}")
            views.append(view)
        result: dict[str, Any] = {"runs": views, "settled": not still_running}
        if still_running:
            # I8: time ran out — say so; never present a live run as done.
            result["guidance"] = (
                f"time ran out after {timeout}s and {still_running} are still "
                "running — their states above are live snapshots, not results. "
                "Call await_runs again to keep waiting."
            )
        elif settled_guidance:
            # The per-run scripts (failed, pending_approval, unconfirmed
            # re-verification, unlanded patch) also ride the top-level guidance
            # so a Queen that reads only this field still sees them.
            result["guidance"] = " ".join(settled_guidance)
        return result
    if name == "list_runs":
        limit = min(int(args.get("limit") or 10), 50)
        views = [_run_view(record, store=store) for record in store.recent_runs(limit)]
        listing: dict[str, Any] = {"runs": views}
        if any(view.get("unlanded_patch") for view in views):
            # v59-F1: same law as the v22-F4 get_run coaching — finished work
            # is not on any branch until landed, and the next step is ALWAYS
            # the landing approval, never another run.
            listing["guidance"] = (
                "Runs with unlanded_patch=true completed but their work is NOT on any "
                "branch yet — landing IS how skep commits. The next step for each is "
                "the land_run action (branch=<the project's auto_apply_branch> if the "
                "user wants the integration branch). "
                "Never dispatch another run to redo, 'stage', or 'commit' finished work."
            )
        return listing
    if name == "get_run":
        run = actions.require_run(store, str(args["task_id"]))
        task_id = str(run["task_id"])
        run["instructions"] = str(run["instructions"])[:500]
        if run["state"] == "completed":
            # v87-F4 (I2): the deliverable's actual content rides the same
            # result as the state — report what you read, not what you were
            # told. None (patchless) stays honestly absent.
            digest = actions.patch_digest(store, task_id)
            if digest is not None:
                run["patch_digest"] = digest
        reverify = store.reverification_for(task_id)
        usage = store.usage_for(task_id)
        events = actions.current_events(store, task_id)
        created = actions.created_transition_views_for_task(store, task_id)
        transitions = [
            {"state": state, "detail": actions.transition_detail_view(detail), "ts": ts}
            for state, detail, ts in store.transitions_for(task_id)
        ]
        project_context = created.get("project_context")
        dispatch_decision = created.get("dispatch_decision")
        landing_decision = created.get("landing_decision")
        if project_context is None:
            for transition in transitions:
                detail = transition["detail"]
                if not isinstance(detail, dict):
                    continue
                project_context = actions.project_context_detail_view(detail.get("project_context"))
                if project_context is not None:
                    break
        detail = {
            "run": run,
            "project_context": project_context,
            "dispatch_decision": dispatch_decision,
            "landing_decision": landing_decision,
            "transitions": transitions,
            "artifacts": [
                {"kind": kind, "path": path, "sha256": sha256}
                for kind, path, sha256 in store.artifacts_for(task_id)
            ],
            "commands": actions.command_views_for_task(store, task_id, events=events),
            "approvals": actions.approval_views(store, task_id, events=events),
            "policy_blocks": actions.policy_block_views(events),
            "applied_branch": actions.applied_branch_for(store, task_id),
            "reverification": None if reverify is None else asdict(reverify),
            "usage": None if usage is None else asdict(usage),
            # v79-F2: run already carries resume_of (asdict); add the forward
            # pointer so the chain is followable from either end.
            "resumed_as": store.resumed_as_for(task_id),
        }
        draft = _draft_excerpt(detail["artifacts"])
        if draft is not None:
            detail["draft"] = draft
        guidance = _get_run_guidance(
            run,
            reverify,
            has_patch=any(a["kind"] == "patch" for a in detail["artifacts"]),
            applied_branch=detail["applied_branch"],
        )
        # v19-F12: append the specific remediation hint so the chat relays a
        # concrete next step instead of the raw error.
        # v23-F3: relay the unbound-repo hint so the Queen offers setup_project.
        if (
            isinstance(dispatch_decision, dict)
            and dispatch_decision.get("detail") == "no project binding; global defaults"
        ):
            guidance = (
                f"{guidance} {actions.UNBOUND_REPO_HINT}" if guidance else actions.UNBOUND_REPO_HINT
            )
        hint = remediation_for(run.get("verification_details")) or remediation_for(
            run.get("summary")
        )
        if hint is not None:
            guidance = f"{guidance} {hint}" if guidance else hint
        if guidance is not None:
            detail["guidance"] = guidance
        return detail
    if name == "repo_state":
        return actions.repo_state_view(holder, str(args["repo"]), store=store)
    if name == "git_log":
        return actions.git_log_view(
            holder,
            str(args["repo"]),
            ref=None if args.get("ref") is None else str(args["ref"]),
            count=int(args.get("count") or 20),
            store=store,
        )
    if name == "git_diff":
        return actions.git_diff_view(
            holder,
            str(args["repo"]),
            base=None if args.get("base") is None else str(args["base"]),
            head=None if args.get("head") is None else str(args["head"]),
            store=store,
        )
    if name == "list_worktrees":
        return actions.list_worktrees_view(holder, store, str(args["repo"]))
    if name == "list_prs":
        return actions.list_prs_view(
            holder, str(args["repo"]), state=str(args.get("state") or "open"), store=store
        )
    if name == "effective_policy":
        return actions.effective_policy_view(holder, store, str(args["repo"]))
    if name == "list_approvals":
        approvals = []
        for approval in store.pending_approvals():
            record = store.get_run(approval.task_id)
            view = actions.approval_view(
                store,
                approval,
                events=actions.current_events(store, approval.task_id),
                project_context=actions.project_context_for_task(store, approval.task_id),
            )
            view["run"] = None if record is None else _run_view(record, store=store)
            approvals.append(view)
        # v79-F2 (I13): the resolved tail rides along — an empty pending queue
        # plus a fresh verdict here is the honest answer to "there is no
        # approval here"; resumed_as on the run closes the chain.
        recently_resolved = [
            {
                "review_id": a.review_id,
                "task_id": a.task_id,
                "action": a.action,
                "status": a.status,
                "resolved_by": a.resolved_by,
                "decided_by": a.decided_by,
                "landing_branch": a.landing_branch,
                "resolved_at": a.resolved_at,
                "resumed_as": store.resumed_as_for(a.task_id),
            }
            for a in store.resolved_approvals()
        ]
        return {"approvals": approvals, "recently_resolved": recently_resolved}
    if name == "get_policy":
        return policy_view(store, holder.current)
    if name == "list_templates":
        return {
            "templates": [
                {
                    "name": t.name,
                    "description": t.description,
                    "caste": t.worker_kind,
                    "params": [p.name for p in t.params],
                    "provenance": t.provenance,
                }
                for t in store.list_templates()
            ]
        }
    if name == "list_skills":
        # v85-F5: candidates (learned drafts) + packs (script-shipping
        # SKILL.md packages on the v17 ladder) in one honest listing.
        from ..skill_packs import load_packs

        return {
            "skills": [
                {
                    "name": c.name,
                    "status": c.status,
                    "occurrences": c.occurrences,
                    "test_outcome": c.test_outcome,
                    "registry_name": c.registry_name,
                }
                for c in store.list_candidates()
            ],
            "packs": [
                {
                    "pack_id": record.pack_id,
                    "state": record.state,
                    "description": record.description,
                    "scripts": list(record.scripts),
                    "grants": list(record.grants),
                    "origin": record.origin,
                    "trial_ok": None if record.trial is None else record.trial.get("ok"),
                }
                for record in load_packs(store).values()
            ],
        }
    if name == "view_skill":
        # v51-F4: the full recipe INCLUDING the grant surface — the same
        # honesty bar as the CLI import paths (v31).
        from ..skill_bundle import grants_summary, skill_grants

        template = store.get_template(str(args["name"]))
        if template is None:
            return {"error": f"no skill/template named {str(args['name'])!r}"}
        return {
            "name": template.name,
            "description": template.description,
            "caste": template.worker_kind,
            "instructions": template.instructions,
            "params": [{"name": p.name, "default": p.default} for p in template.params],
            "provenance": template.provenance,
            "grants": grants_summary(skill_grants(template)),
        }
    if name == "list_schedules":
        # v73-F7: compact by default — the full list (instructions +
        # per-schedule context for every row) measured 10.5KB against the 8KB
        # replay cap in the field, so every model saw a mid-JSON chop and
        # looped. name=<schedule> is the detail view (v70-F4's recipe access
        # lives there now).
        if args.get("name"):
            schedule = store.get_schedule(str(args["name"]))
            if schedule is None:
                return {
                    "error": f"no schedule named {str(args['name'])!r} — "
                    "call list_schedules with no arguments to see them"
                }
            return {
                "schedule": {
                    **_schedule_summary(schedule),
                    "instructions": str(schedule.instructions)[:2000],
                    "last_output": (
                        None if schedule.last_output is None else str(schedule.last_output)[:2000]
                    ),
                    "repo": schedule.repo,
                    "template": schedule.template_name,
                    "chain": schedule.chain,
                    "once": schedule.once,
                    "project_context": actions.project_context_for_schedule(store, schedule),
                }
            }
        return {"schedules": [_schedule_summary(s) for s in store.list_schedules()]}
    if name == "list_repos":
        # v73-F3 / v81-F10: clones + workon-bound dirs, via the shared helper
        # every repo surface answers with.
        return {"repos": known_repos(repos_root(holder), store)}
    if name == "list_projects":
        return {"projects": [project_to_dict(project) for project in list_projects(store)]}
    if name == "list_providers":
        return {"providers": [asdict(p) for p in store.list_provider_profiles()]}
    if name == "list_provider_presets":
        from ..provider_presets import PROVIDER_PRESETS, preset_view

        return {"presets": [preset_view(p) for p in PROVIDER_PRESETS.values()]}
    if name == "list_policy_groups":
        return actions.list_policy_groups(store)
    if name == "list_notes":
        # v81-F8: unpaged notes met the replay truncator and the OLDEST notes
        # silently vanished — page, and always say how many exist.
        notes = [asdict(note) for note in store.list_notes()]
        offset = max(int(args.get("offset") or 0), 0)
        limit = max(int(args.get("limit") or 20), 1)
        page = notes[offset : offset + limit]
        return {"notes": page, "total": len(notes), "shown": len(page), "offset": offset}
    if name == "list_tasks":
        return {"tasks": [asdict(task) for task in store.list_tasks()]}
    if name == "add_note":
        note = store.create_note(str(args["content"]).strip(), actor=CHAT_TOOL_ACTOR)
        return {"note": asdict(note)}
    if name == "add_task":
        task = store.create_task(str(args["title"]).strip(), actor=CHAT_TOOL_ACTOR)
        return {"task": asdict(task)}
    if name == "complete_task":
        task_id = str(args["task_id"])
        current = store.get_task(task_id)
        if current is None:
            return {"error": f"no task {task_id!r}"}
        updated_task = store.update_task(
            task_id,
            title=current.title,
            status="done",
            due_at=current.due_at,
            actor=CHAT_TOOL_ACTOR,
            action="completed",
        )
        return {"task": None if updated_task is None else asdict(updated_task)}
    if name == "reopen_task":
        # v81-F7: the inverse of complete_task — a landing todo wrongly marked
        # done gets reopened, not duplicated.
        task_id = str(args["task_id"])
        current = store.get_task(task_id)
        if current is None:
            return {"error": f"no task {task_id!r}"}
        updated_task = store.update_task(
            task_id,
            title=current.title,
            status="todo",
            due_at=current.due_at,
            actor=CHAT_TOOL_ACTOR,
            action="reopened",
        )
        return {"task": None if updated_task is None else asdict(updated_task)}
    if name == "list_memory":
        project = None if args.get("project") is None else str(args["project"])
        items = store.list_memory_items(project_id=project)
        return {"items": [asdict(item) for item in items]}
    if name == "search_memory":
        project = None if args.get("project") is None else str(args["project"])
        hits = store.search_memory(str(args["query"]), project_id=project)
        return {"items": [asdict(item) for item in hits]}
    if name == "search_chats":
        limit = min(int(args.get("limit") or 20), 50)
        scope = None if args.get("chat_id") is None else str(args["chat_id"])
        return {
            "hits": [
                _search_hit_payload(hit)
                for hit in store.search_chats(str(args["query"]), limit=limit, chat_id=scope)
            ]
        }
    if name == "list_chats":
        # v53-F3: session-level browse — search finds content, this lists
        # sessions.
        return {"chats": store.chat_overviews(limit=min(int(args.get("limit") or 10), 50))}
    if name == "get_chat_messages":
        # v53-F3: the scroll tool. Content is truncated per message so one
        # verbose transcript cannot flood the small model's context.
        records = store.chat_messages(
            str(args["chat_id"]),
            limit=min(int(args.get("limit") or 20), 50),
            offset=max(int(args.get("offset") or 0), 0),
        )
        return {
            "messages": [
                {
                    "role": record.role,
                    "content": record.content[:500] + (" …" if len(record.content) > 500 else ""),
                    "tool_name": record.tool_name,
                    "created_at": record.created_at,
                }
                for record in records
            ]
        }
    if name == "list_processes":
        from . import processes

        return processes.list_processes(store)
    if name == "read_process_log":
        from . import processes

        return processes.read_process_log(
            store, str(args["proc_id"]), tail=_optional_int(args, "tail")
        )
    if name == "get_chat_context":
        # v83-F3: the window around a search hit. Same per-message truncation
        # as get_chat_messages; ids ride along so the Queen can page further.
        records = store.chat_messages_around(
            str(args["chat_id"]),
            int(args["message_id"]),
            before=min(max(int(args.get("before") or 10), 0), 25),
            after=min(max(int(args.get("after") or 10), 0), 25),
        )
        return {
            "messages": [
                {
                    "id": record.id,
                    "role": record.role,
                    "content": record.content[:500] + (" …" if len(record.content) > 500 else ""),
                    "tool_name": record.tool_name,
                    "created_at": record.created_at,
                }
                for record in records
            ]
        }
    if name == CLARIFY_TOOL_NAME:
        # The chat engine intercepts this in its turn loop (it ends the
        # turn); a non-chat caller reaching here gets an honest inert answer.
        return {
            "asked": str(args.get("question") or ""),
            "note": "the user's next message answers this",
        }
    if name == "search_web":
        # v44-F8: Queen-side discovery — a failed search is a clean tool error,
        # never an exception into the turn loop (the small model handles text).
        # v52-F3: the operator policy decides first (network/search — allowed
        # by the default net:search rule; an operator deny stops search here).
        # Read tools never card: anything but allow is a clean policy error.
        from ..policy_resolver import resolve_operator_policy
        from . import websearch

        search_decision = resolve_operator_policy(store).decision("network", "search", "ddgs")
        if search_decision.verdict != "allow":
            return {
                "error": "web search is not allowed by the operator policy "
                f"(decided_by: {search_decision.decided_by})",
                "results": [],
            }
        try:
            results = websearch.search_web(
                str(args["query"]), max_results=int(args.get("max_results") or 5)
            )
        except Exception as exc:
            return {"error": f"web search failed: {exc}", "results": []}
        return {"results": results, "decided_by": search_decision.decided_by}
    if name == "list_memory_proposals":
        state = None if args.get("state") is None else str(args["state"])
        try:
            proposals = store.list_memory_proposals(state=state)
        except MemoryError as exc:
            return {"error": str(exc)}
        return {"proposals": [asdict(p) for p in proposals]}
    return {"error": f"unknown tool {name!r}"}


# v56-F4: verbs that reveal which project a chat is working on.
_PROJECT_BINDING_TOOLS = frozenset(
    {"dispatch_run", "run_code", "batch_dispatch", "workon", "setup_project"}
)


def _bind_chat_project(
    store: RunStore,
    holder: ConfigHolder | None,
    chat_id: str,
    name: str,
    args: dict[str, Any],
    result: Any,
) -> None:
    """Remember the chat's project so its scoped memory rides the prompt
    (v56-F4). Best-effort — binding must never fail the verb it rides."""
    if name not in _PROJECT_BINDING_TOOLS:
        return
    try:
        project_id = None
        if isinstance(result, dict):
            project = result.get("project")
            project_id = result.get("project_id") or (
                project.get("project_id") if isinstance(project, dict) else None
            )
        if project_id is None and args.get("repo"):
            repo = str(args["repo"])
            record = store.project_for_binding("repo_slug", repo)
            if record is None and holder is not None:
                resolved = resolve_repo_arg(repo, repos_root(holder), store)
                record = store.project_for_binding("repo_path", str(resolved))
            project_id = record.project_id if record is not None else None
        if project_id:
            store.set_chat_project(chat_id, str(project_id))
    except Exception:
        pass


def _queen_landing_branch(store: RunStore, run: dict[str, Any], raw: Any) -> str | None:
    """v81-F4: from chat, the landing branch is picked from a menu, not a hat.

    Legal targets are ``skep/<task_id>`` and the project's configured
    ``auto_apply_branch`` — naming the target is part of the trigger (I6), so
    a model-invented name (``skep/glm-5.2``) is rejected with the menu.
    Free-form names stay operator-only (web UI / CLI, v20-F5).
    """
    if raw is None:
        return None
    branch = str(raw).strip()
    if not branch:
        return None
    task_id = str(run["task_id"])
    legal = {f"skep/{task_id}"}
    repo = Path(str(run["repo"]))
    for kind, value in (("repo_slug", repo.name), ("repo_path", str(repo))):
        project = store.project_for_binding(kind, value)
        if project is not None and project.policy.get("auto_apply_branch"):
            legal.add(str(project.policy["auto_apply_branch"]))
    if branch not in legal:
        raise HTTPException(
            status_code=400,
            detail=f"branch {branch!r} is not a landing target this chat may name — "
            "use one of: " + ", ".join(sorted(legal)) + " (or omit branch for the "
            "default). Free-form branch names are operator-only, via the web UI or CLI.",
        )
    return branch


def execute_mutation(
    name: str,
    args: dict[str, Any],
    *,
    store: RunStore,
    holder: ConfigHolder,
    runner: Dispatcher,
    actor: str,
    decision: AutonomyDecision | None = None,
    chat_id: str | None = None,
) -> Any:
    """Run one confirmed mutation through the shared supervisor verbs.

    ``chat_id`` is the chat the mutation was confirmed in, when there is one —
    note schedules bind to it so their ticks deliver into that chat (v43-F6).
    """
    result = _execute_mutation(
        name,
        args,
        store=store,
        holder=holder,
        runner=runner,
        actor=actor,
        decision=decision,
        chat_id=chat_id,
    )
    if chat_id is not None:
        _bind_chat_project(store, holder, chat_id, name, args, result)
    return result


def _execute_mutation(
    name: str,
    args: dict[str, Any],
    *,
    store: RunStore,
    holder: ConfigHolder,
    runner: Dispatcher,
    actor: str,
    decision: AutonomyDecision | None = None,
    chat_id: str | None = None,
) -> Any:
    if name == "register_mcp_server":
        from ..mcp_client import MCPError, MCPServerConfig, save_mcp_server

        config = MCPServerConfig(
            server_id=str(args["server_id"]),
            transport=str(args["transport"]),
            command=tuple(str(part) for part in args.get("command") or ()),
            url=None if args.get("url") is None else str(args["url"]),
            scope=str(args.get("scope") or "mcp"),
        )
        try:
            save_mcp_server(store, config)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc
        return {
            "registered": config.server_id,
            "transport": config.transport,
            "command": list(config.command),
            "url": config.url,
            "scope": config.scope,
        }
    if name == "read_url":
        # v47-F4: Queen search ≠ Queen fetch — the fetch happens HERE, strictly
        # after the card confirm; a failed read degrades to a clean tool error.
        # v52-F3 (Option A): the card IS the human gate; the operator policy is
        # consulted for AUDIT only — an explicit allow's rule id rides the
        # result, otherwise decided_by names the card. Never blocks a confirm.
        import urllib.parse

        from ..policy_resolver import resolve_operator_policy
        from . import websearch

        url = str(args["url"])
        host = urllib.parse.urlparse(url).hostname or ""
        # v72-F7: on the granted-domain lane the standing grant is the gate —
        # every redirect hop re-decides against it (a redirect must never
        # widen a grant; fail closed off-domain).
        granted = (
            decision is not None
            and decision.reason == "network.allow.fetch_grant"
            and decision.allows_execution()
        )
        redirect_guard = None
        if granted:

            def _hop_allowed(candidate_host: str) -> bool:
                hop = fetch_grant_decision(store, candidate_host)
                return hop is not None and hop.allows_execution()

            redirect_guard = _hop_allowed
            decided_by = decision.decided_by if decision is not None else "operator-card"
        else:
            connect = resolve_operator_policy(store).decision("network", "connect", host)
            decided_by = connect.decided_by if connect.verdict == "allow" else "operator-card"
        # v83-F1: the granted lane earned the bigger budget; the card lane
        # stays small so a card review stays cheap. Markdown by default.
        try:
            return {
                **websearch.fetch_url_text(
                    url,
                    redirect_guard=redirect_guard,
                    markdown=str(args.get("mode") or "markdown") != "text",
                    max_bytes=websearch.GRANTED_READ_MAX_BYTES
                    if granted
                    else websearch.READ_URL_MAX_BYTES,
                    max_chars=websearch.GRANTED_READ_MAX_CHARS
                    if granted
                    else websearch.READ_URL_MAX_CHARS,
                ),
                "decided_by": decided_by,
            }
        except ValueError:
            raise
        except Exception as exc:
            return {"error": f"read_url failed: {exc}", "url": url}
    if name == "run_shell":
        # v83-F9: supervisor-side with operator standing — the card (or a
        # standing shell rule) is the gate. The guards re-check HERE (the
        # read_file last-guard pattern): a confirmed card must still never
        # push/commit/switch/sudo, and a repo cwd the rule lane refused
        # stays refused on confirm (denied space unreachable by confirmation).
        import shlex
        import subprocess

        shell_command = str(args["command"])
        shell_cwd = None if args.get("cwd") is None else str(args["cwd"])
        try:
            if not shlex.split(shell_command):
                raise ValueError("command must be a non-empty shell command")
        except ValueError as exc:
            raise ValueError(f"command could not be parsed: {exc}") from exc
        last_guard = decision or queen_shell_decision(
            store, holder, command=shell_command, cwd=shell_cwd
        )
        if last_guard is not None and last_guard.verdict == "deny":
            raise ValueError(last_guard.detail or "denied by policy")
        shell_timeout = min(
            int(args.get("timeout") or RUN_SHELL_TIMEOUT_CAP), RUN_SHELL_TIMEOUT_CAP
        )
        from pathlib import Path as _Path

        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", shell_command],
                capture_output=True,
                text=True,
                timeout=shell_timeout,
                cwd=None if shell_cwd is None else str(_Path(shell_cwd).expanduser()),
            )
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {shell_timeout}s", "command": shell_command}
        except OSError as exc:
            return {"error": f"failed to start: {exc}", "command": shell_command}

        def _shell_cap(text: str) -> str:
            if len(text) <= RUN_SHELL_OUTPUT_CAP:
                return text
            return text[:RUN_SHELL_OUTPUT_CAP] + "\n… (truncated)"

        shell_result: dict[str, Any] = {
            "command": shell_command,
            "exit_code": proc.returncode,
            "output": _shell_cap(proc.stdout or ""),
        }
        if proc.stderr:
            shell_result["stderr"] = _shell_cap(proc.stderr)
        if last_guard is not None:
            shell_result["decided_by"] = last_guard.decided_by
        return shell_result
    if name == "setup_browser":
        # v83-F11: the manual npx incantation nobody ran, as one card. The
        # browse scope's read/act split (v71-F2) does the governing; this
        # only registers the documented server. Handshake is best-effort —
        # a missing npx reports honestly instead of leaving a dead entry
        # silently registered (I8).
        from ..mcp_client import (
            MCPClient,
            MCPError,
            MCPServerConfig,
            runner_for_config,
            save_mcp_server,
        )

        browser_config = MCPServerConfig(
            server_id="browser",
            transport="stdio",
            command=("npx", "@playwright/mcp@latest"),
            url=None,
            scope="browse",
        )
        try:
            save_mcp_server(store, browser_config)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc
        result_view: dict[str, Any] = {
            "registered": "browser",
            "scope": "browse",
            "command": list(browser_config.command),
            "ramp": (
                "page-state reads flow free; acting tools (navigate/click/"
                "type) card until allow_mcp_tool grants each by name"
            ),
        }
        try:
            handshake = MCPClient(
                browser_config, runner=runner_for_config(browser_config)
            ).list_tools()
            result_view["tools"] = [tool.name for tool in handshake]
        except MCPError as exc:
            result_view["handshake_failed"] = (
                f"{exc} — the server is registered but not reachable; npx "
                "(Node.js) must be installed on this host"
            )
        return result_view
    if name == "quick_edit":
        # v83-F10: write_file/patch parity, governed — sugar over a single-
        # file coding dispatch. The Queen still never holds a pen (I3) and
        # the landing card is the same one every run gets (I1); the value is
        # the packaging: one hop from 'fix the typo' to a landable patch.
        edit_file = str(args["file"]).strip()
        edit_instruction = str(args["instruction"]).strip()
        if not edit_file or not edit_instruction:
            raise ValueError("quick_edit needs a file and a plain instruction")
        task_id = actions.submit_run(
            holder,
            runner,
            store,
            repo=str(args["repo"]),
            instructions=(
                f"Quick edit — ONE file only: {edit_file}\n"
                f"Change: {edit_instruction}\n"
                "Touch no other file. Verify first-class (state the check "
                "you ran): the file still parses/imports, or the smallest "
                "existing test covering it still passes."
            ),
            caste="coding",
            dispatch_decision=decision,
        )
        return {
            "task_id": task_id,
            "file": edit_file,
            "note": "single-file run dispatched; it lands as a patch through its own approval",
        }
    if name == "start_process":
        # v83-F8: same last-guard shape as run_shell — a confirmed card must
        # still never open a repo cwd or a guarded command.
        from . import processes as proc_mod

        start_command = str(args["command"])
        start_cwd = None if args.get("cwd") is None else str(args["cwd"])
        start_guard = decision or queen_shell_decision(
            store, holder, command=start_command, cwd=start_cwd, background=True
        )
        if start_guard is not None and start_guard.verdict == "deny":
            raise ValueError(start_guard.detail or "denied by policy")
        return proc_mod.start_process(
            store, holder.current.home, command=start_command, cwd=start_cwd
        )
    if name == "stop_process":
        from . import processes as proc_mod

        return proc_mod.stop_process(store, str(args["proc_id"]))
    if name == "resume_run":
        # v72-F8 (R8): the operator's confirmed card is the only trigger (I6);
        # the existing resume seam does the work; landing rules unchanged (I1).
        return actions.resume_crashed_run(
            store, holder.current, runner, str(args["task_id"]), actor
        )
    if name == "diagnose_run":
        # v107-F2: one bounded command in the kept evidence — sandboxed like
        # re-verification (I5), always carded (I6), reads the tree it names.
        return actions.diagnose_run(
            store,
            holder.current,
            str(args["task_id"]),
            command=str(args["command"]),
            timeout_seconds=float(args["timeout_seconds"])
            if args.get("timeout_seconds") is not None
            else None,
        )
    if name == "delegate_analysis":
        # v83-F7 (ADR 0041): reasoning-only delegation — read-only LLM turns
        # recorded as ordinary chats (I8: the transcript IS the record and
        # search_chats reaches it). No worktree, no sandbox, nothing to land;
        # a mutation attempted inside an analyst turn refuses (read_only), so
        # an analyst can never nest a delegation or card anything.
        from .chat import run_analysis_tasks

        raw_analysis = args.get("tasks")
        if not isinstance(raw_analysis, list) or not raw_analysis:
            raise ValueError("tasks must be a non-empty array of analysis prompts")
        if len(raw_analysis) > ANALYSIS_CAP:
            raise ValueError(
                f"delegate_analysis caps at {ANALYSIS_CAP} analysts per call "
                "(ADR 0041) — split the rest into a second call"
            )
        analysis_tasks = [str(entry).strip() for entry in raw_analysis]
        if any(not entry for entry in analysis_tasks):
            raise ValueError("every task must be a non-empty prompt")
        return run_analysis_tasks(
            store,
            holder,
            runner,
            holder.current.home,
            analysis_tasks,
            None if args.get("context") is None else str(args["context"]),
        )
    if name == "batch_dispatch":
        # v51-F5 (ADR 0025): N independent dispatch_runs submitted together.
        # The thread pool runs them concurrently; each gets its own worktree,
        # policy compile, audit trail, and G10 re-verification.
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("tasks must be a non-empty array of {repo, instructions}")
        if len(raw_tasks) > BATCH_DISPATCH_CAP:
            raise ValueError(f"batch_dispatch caps at {BATCH_DISPATCH_CAP} tasks per batch")
        entries: list[dict[str, Any]] = []
        for entry in raw_tasks:
            if not isinstance(entry, dict) or "repo" not in entry or "instructions" not in entry:
                raise ValueError("each task must be an object with repo and instructions")
            entries.append(entry)
        task_ids = [
            actions.submit_run(
                holder,
                runner,
                store,
                repo=str(entry["repo"]),
                instructions=str(entry["instructions"]),
                caste=str(entry.get("caste") or "coding"),
                execution_mode=(
                    None if entry.get("execution_mode") is None else str(entry["execution_mode"])
                ),
                # v98-F1: per-member engine. resolve_engine refuses an unknown
                # name and the v90/v94 verify_command + sandbox guards run per
                # member — the batch takes the single-dispatch path, unchanged.
                engine=(None if entry.get("engine") is None else str(entry["engine"])),
                dispatch_decision=decision,
            )
            for entry in entries
        ]
        return {"dispatched": task_ids, "count": len(task_ids)}
    if name == "create_skill":
        # v51-F4: hand-authored from chat — never the ADR 0016 test gate
        # (nothing was learned), always the human gate (this card). Zero
        # grants by construction: the WorkflowTemplate defaults are empty.
        from ..templates import TemplateError, WorkflowTemplate, validate_template

        skill_name = str(args["name"]).strip()
        template = WorkflowTemplate(
            name=skill_name,
            instructions=str(args["instructions"]),
            description=str(args.get("description") or ""),
            provenance="chat",
        )
        try:
            validate_template(template)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
        if store.get_template(skill_name) is not None:
            raise ValueError(
                f"a skill named {skill_name!r} already exists; "
                "pick another name (no silent overwrite)"
            )
        store.add_template(template)
        return {"created": skill_name, "provenance": "chat"}
    if name == "patch_skill":
        import dataclasses

        from ..templates import TemplateError, validate_template

        skill_name = str(args["name"])
        existing = store.get_template(skill_name)
        if existing is None:
            raise ValueError(f"no skill/template named {skill_name!r}")
        old_string = str(args["old_string"])
        if old_string not in existing.instructions:
            raise ValueError("old_string was not found in the skill's instructions")
        patched = dataclasses.replace(
            existing,
            instructions=existing.instructions.replace(old_string, str(args["new_string"]), 1),
        )
        try:
            validate_template(patched)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
        store.add_template(patched)
        return {"patched": skill_name}
    if name == "delete_skill":
        skill_name = str(args["name"])
        removed_template = store.get_template(skill_name)
        if not store.remove_template(skill_name):
            raise ValueError(f"no skill/template named {skill_name!r}")
        # v83-F12: deleting a seed leaves a tombstone — the startup sync
        # honors it forever, so a restart never resurrects the delete (I8).
        if removed_template is not None and removed_template.provenance == "seed":
            from ..seed_skills import add_seed_tombstone

            add_seed_tombstone(store, skill_name)
        return {"deleted": skill_name}
    if name == "run_code":
        # v51-F3 (ADR 0024): the code runs as a SANDBOXED script-worker run —
        # deny-all egress, workspace-only writes, full event evidence — never
        # supervisor-side. The dispatch blocks until the run finishes so the
        # script's output IS the tool result.
        # v83-F2: fast=true runs supervisor-side in the SAME walls (native
        # sandbox, tmp workspace, deny-all net, 10s) — no usable backend →
        # the worker dispatch below, never an unsandboxed run (I12), with
        # the fallback reason named (I9).
        from skep.workers.script_worker import script_instructions

        language = str(args.get("language") or "python")
        fast_requested = bool(args.get("fast"))
        if fast_requested:
            from .fastlane import run_code_fast

            fast = run_code_fast(language, str(args["code"]))
            if fast is not None:
                return fast
        # v106-F7: the caller may raise the wall clock up to the ceiling —
        # npm-driving scripts outlive the 120s smoke default.
        wall_clock = min(
            int(args.get("timeout_seconds") or SCRIPT_RUN_WALL_CLOCK_SECONDS),
            SCRIPT_RUN_MAX_WALL_CLOCK_SECONDS,
        )
        if wall_clock <= 0:
            wall_clock = SCRIPT_RUN_WALL_CLOCK_SECONDS
        task_id = actions.submit_run(
            holder,
            runner,
            store,
            repo=str(args["repo"]),
            instructions=script_instructions(language, str(args["code"])),
            caste="script",
            execution_mode="sandbox",
            network=[],  # deny-all egress: a script computes, it never phones out
            wall_clock_seconds=wall_clock,
            dispatch_decision=decision,
        )
        result = _script_run_result(store, task_id, wall_clock_seconds=wall_clock)
        if fast_requested:
            from .fastlane import FAST_LANE_FALLBACK_NOTE

            result = {**result, "fast_lane_fallback": FAST_LANE_FALLBACK_NOTE}
        return result
    if name in {"read_file", "search_files"}:
        # v51-F2: Queen file reads. Last guard mirrors call_mcp_tool — a deny
        # rule refuses even on a confirmed card; denied space stays
        # unreachable by confirmation.
        from .fileio import (
            queen_filesystem_decision,
            read_file_branch_aware,
            search_files_result,
        )

        last_guard = queen_filesystem_decision(
            store, holder, action="read", path=str(args.get("path") or "")
        )
        if last_guard.verdict == "deny":
            raise ValueError(f"denied by policy: {last_guard.decided_by or last_guard.reason}")
        # v52-F5: the rule that admitted the read rides the result — the
        # transcript is the audit record (ADR 0019 §3).
        if name == "read_file":
            # v79-F3: ref-aware — the guard above already decided the PATH;
            # a git-show read of that same path stays inside the boundary (I5).
            return {
                **read_file_branch_aware(
                    store,
                    str(args["path"]),
                    ref=None if args.get("ref") is None else str(args["ref"]),
                    offset=_optional_int(args, "offset"),
                    limit=_optional_int(args, "limit"),
                ),
                "decided_by": last_guard.decided_by,
            }
        return {
            **search_files_result(
                str(args["pattern"]),
                path=str(args["path"]),
                target=str(args.get("target") or "content"),
                file_glob=None if args.get("file_glob") is None else str(args["file_glob"]),
            ),
            "decided_by": last_guard.decided_by,
        }
    if name == "sync_notes":
        # v71-F4: a vault is a directory of markdown — stdlib file writes on
        # the card the operator just confirmed; the clobber guard in
        # obsidian.sync_notes keeps hand-edits sacred (I8).
        from ..obsidian import OBSIDIAN_VAULT_SETTINGS_KEY, resolve_vault, sync_notes

        raw_path = str(args.get("vault_path") or "").strip()
        if not raw_path:
            remembered = store.get_setting(OBSIDIAN_VAULT_SETTINGS_KEY)
            if not isinstance(remembered, str) or not remembered:
                raise ValueError(
                    "no vault is configured yet — pass vault_path (absolute or "
                    "~/... path of the Obsidian vault folder); it is remembered "
                    "after the first confirmed sync"
                )
            raw_path = remembered
        vault = resolve_vault(raw_path)
        report = sync_notes(store, vault)
        store.set_setting(OBSIDIAN_VAULT_SETTINGS_KEY, str(vault))
        return report
    if name == "forge_tool":
        # v71-F1: authoring is a NORMAL dispatch into the operator-owned forge
        # repo — the patch lands only via the human approval (I1), and landing
        # still activates nothing (promote_tool is the gate that runs code).
        from .. import forge

        tool_title = str(args["name"]).strip()
        purpose = str(args["purpose"]).strip()
        if not purpose:
            raise ValueError("purpose is the worker's brief — say what the tool must do")
        plugin_id = forge.plugin_slug(tool_title)
        live_plugin = forge.load_plugins(store).get(plugin_id)
        if live_plugin is not None and live_plugin.state in {"active", "suspended"}:
            raise ValueError(
                f"plugin {plugin_id!r} is {live_plugin.state} — suspend_tool with "
                "rollback=true retires it first; then forge the replacement"
            )
        root = forge.forge_root(holder.current)
        root.mkdir(parents=True, exist_ok=True)
        forge.ensure_forge_seed(root)
        # The workon on-ramp is idempotent for the same path: git baseline +
        # trusted project record, so the dispatch below is an ordinary run.
        actions.workon(holder, store, path=str(root))
        rel_path = f"tools/{plugin_id}.py"
        task_id = actions.submit_run(
            holder,
            runner,
            store,
            repo=str(root),
            instructions=forge.authoring_instructions(tool_title, purpose, rel_path),
            caste="coding",
            dispatch_decision=decision,
        )
        forge.save_plugin(
            store,
            forge.ForgedPlugin(
                plugin_id=plugin_id,
                name=tool_title,
                purpose=purpose,
                state="draft",
                repo=str(root),
                rel_path=rel_path,
                task_id=task_id,
                server_id=f"forge-{plugin_id}",
            ),
        )
        return {
            "forged": plugin_id,
            "task_id": task_id,
            "state": "draft",
            "next": "review and approve the run's patch to land the tool, then promote_tool",
        }
    if name == "promote_tool":
        # v71-F1: the lifecycle ladder, driven for real — every transition goes
        # through require_transition, so the v17 gates (G10-verified trial for
        # `tested`, a human action for `approved`) are enforced by shape, not
        # by this branch remembering to check.
        import dataclasses
        import sys as _sys
        from pathlib import Path

        from .. import forge
        from ..mcp_client import MCPError, MCPServerConfig, save_mcp_server
        from ..plugin_lifecycle import require_transition

        plugin_id = str(args["plugin_id"])
        plugins = forge.load_plugins(store)
        record = plugins.get(plugin_id)
        if record is None:
            known = ", ".join(sorted(plugins)) or "none yet — forge_tool creates one"
            raise ValueError(f"no forged plugin {plugin_id!r}; known: {known}")
        if record.state == "active":
            return {"plugin_id": plugin_id, "state": "active", "note": "already active"}
        if record.state == "rolled_back":
            raise ValueError(
                f"plugin {plugin_id!r} was rolled back — that is terminal; forge_tool a replacement"
            )
        trial_repo = record.repo
        branch: str | None = None
        if record.provenance == "seed":
            # v83-F14: a shipped seed tool has no authoring run — its source
            # of truth is the versioned package file. The SAME sandboxed
            # trial and the SAME confirmed card gate it below; only the
            # source location differs. The trial runs in the forge repo
            # (prepared exactly like forge_tool prepares it).
            source = forge.seed_tool_source(record.rel_path)
            forge_repo = forge.forge_root(holder.current)
            forge_repo.mkdir(parents=True, exist_ok=True)
            forge.ensure_forge_seed(forge_repo)
            actions.workon(holder, store, path=str(forge_repo))
            trial_repo = str(forge_repo)
        else:
            branch = actions.applied_branch_for(store, record.task_id)
            if branch is None:
                raise ValueError(
                    f"the authoring run {record.task_id} has not landed — review and "
                    "approve its patch first (approve_review) so there is approved "
                    "source to promote"
                )
            source = forge.landed_source(Path(record.repo), branch, record.rel_path)
        if record.state == "suspended":
            # Reactivation: the pause ends on this confirmed card; the source
            # was already trialed and approved, so only the registration returns.
            require_transition("suspended", "active")
        else:
            if record.state == "draft":
                require_transition("draft", "sandboxed")
                record = dataclasses.replace(record, state="sandboxed")
                forge.save_plugin(store, record)
            trial_result = _forge_trial(
                store, holder, runner, source=source, repo=trial_repo, decision=decision
            )
            ok, reason, evidence = forge.trial_verdict(trial_result)
            record = dataclasses.replace(record, trial=evidence)
            forge.save_plugin(store, record)
            if not ok:
                raise ValueError(
                    f"the sandboxed trial did not pass: {reason} — the plugin stays "
                    "'sandboxed'. Fix the tool in the forge repo (forge_tool again "
                    "re-briefs a worker), land the fix, then promote_tool again."
                )
            require_transition("sandboxed", "tested", verifier_passed=True)
            require_transition("tested", "reviewed")
            # The card the operator just confirmed IS the human action (I6/I7
            # — asked once, never re-asked).
            require_transition("reviewed", "approved", human_action=True)
            require_transition("approved", "active")
        installed = forge.install_source(holder.current, plugin_id, source)
        try:
            save_mcp_server(
                store,
                MCPServerConfig(
                    server_id=record.server_id,
                    transport="stdio",
                    command=(_sys.executable, str(installed)),
                ),
            )
        except MCPError as exc:
            raise ValueError(str(exc)) from exc
        record = dataclasses.replace(record, state="active")
        forge.save_plugin(store, record)
        return {
            "plugin_id": plugin_id,
            "state": "active",
            "server_id": record.server_id,
            "landed_branch": branch,
            "installed": str(installed),
            "tools": None if record.trial is None else record.trial.get("tools"),
            "self_test": None if record.trial is None else record.trial.get("self_test"),
        }
    if name == "suspend_tool":
        # v71-F1: suspension IS deregistration — registered ⟺ active, so a
        # suspended tool has no callable surface left anywhere (I5).
        import dataclasses

        from .. import forge
        from ..mcp_client import MCPError, remove_mcp_server
        from ..plugin_lifecycle import PluginLifecycleError, require_transition

        plugin_id = str(args["plugin_id"])
        record = forge.load_plugins(store).get(plugin_id)
        if record is None:
            raise ValueError(f"no forged plugin {plugin_id!r}; list_plugins shows them")
        target = "rolled_back" if bool(args.get("rollback")) else "suspended"
        try:
            require_transition(record.state, target)
        except PluginLifecycleError as exc:
            raise ValueError(
                f"{exc} — suspend pauses an ACTIVE tool; rollback=true retires a "
                "plugin from any non-terminal state"
            ) from exc
        import contextlib

        # MCPError here = never registered (e.g. rolling back a draft).
        with contextlib.suppress(MCPError):
            remove_mcp_server(store, record.server_id)
        forge.save_plugin(store, dataclasses.replace(record, state=target))
        return {"plugin_id": plugin_id, "state": target, "deregistered": record.server_id}
    if name == "promote_skill_pack":
        # v85-F5: the pack ladder from chat — the card the operator just
        # confirmed IS the human action (I6/I7), exactly the promote_tool
        # precedent; every edge still goes through require_transition.
        from ..plugin_lifecycle import PluginLifecycleError
        from ..skill_packs import SkillPackError, promote_pack

        pack_id = str(args["pack_id"])
        extra = tuple(str(g) for g in (args.get("allow_scripts") or ()))
        try:
            pack_record, pack_template = promote_pack(
                store, holder.current, pack_id, extra_grants=extra, human_action=True
            )
        except (SkillPackError, PluginLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        if pack_template is None:
            return {"pack_id": pack_id, "state": "active", "note": "already active"}
        return {
            "pack_id": pack_id,
            "state": pack_record.state,
            "trial": pack_record.trial,
            "grants": [list(command) for command in pack_template.shell_allowlist],
            "note": f"dispatchable as skill {pack_record.pack_id!r} (run_template)",
        }
    if name == "suspend_skill_pack":
        # v85-F5: suspension removes the registry skill — registered ⟺
        # active, so a suspended pack has no dispatchable surface left (I8).
        from ..plugin_lifecycle import PluginLifecycleError
        from ..skill_packs import SkillPackError, suspend_pack

        try:
            pack_record = suspend_pack(
                store, str(args["pack_id"]), rollback=bool(args.get("rollback"))
            )
        except (SkillPackError, PluginLifecycleError) as exc:
            raise ValueError(
                f"{exc} — suspend pauses an ACTIVE pack; rollback=true retires "
                "a pack from any non-terminal state"
            ) from exc
        return {"pack_id": pack_record.pack_id, "state": pack_record.state}
    if name == "unregister_mcp_server":
        # v47-F2: CRUD honesty — the remove half of register_mcp_server.
        from ..mcp_client import MCPError, remove_mcp_server

        try:
            remove_mcp_server(store, str(args["server_id"]))
        except MCPError as exc:
            raise ValueError(str(exc)) from exc
        return {"unregistered": str(args["server_id"])}
    if name == "call_mcp_tool":
        from ..mcp_client import (
            MCPClient,
            MCPError,
            MCPTool,
            load_mcp_servers,
            mcp_scope_decision,
            runner_for_config,
        )

        server_id = str(args["server_id"])
        tool_name = str(args["tool"])
        server_config = load_mcp_servers(store).get(server_id)
        if server_config is None:
            raise ValueError(f"no MCP server {server_id!r}; register_mcp_server first")
        # Last guard: a deny rule refuses even on a confirmed card — denied
        # space stays unreachable by confirmation.
        last_guard = mcp_scope_decision(
            store, MCPTool(server_id=server_id, name=tool_name, description="")
        )
        if last_guard.verdict == "deny":
            raise ValueError(f"denied by policy: {last_guard.decided_by or last_guard.reason}")
        try:
            client = MCPClient(server_config, runner=runner_for_config(server_config))
            call = client.call_tool(tool_name, _object_arg(args, "arguments"))
        except MCPError as exc:
            raise ValueError(str(exc)) from exc
        if not call.ok:
            return {"server_id": server_id, "tool": tool_name, "ok": False, "error": call.error}
        return {"server_id": server_id, "tool": tool_name, "ok": True, "content": call.content}
    if name == "allow_fetch_domain":
        # v72-F7: the allow_mcp_tool pattern, for the read_url lane — one
        # vetted learned rule in the one engine; deny always wins (v40).

        domain = str(args["domain"]).strip().lower().rstrip(".")
        if not domain or "*" in domain or any(ch in domain for ch in "/:@ ") or "." not in domain:
            raise ValueError(
                "domain must be a bare hostname like 'docs.python.org' — no scheme, "
                "path, port, or wildcard"
            )
        # v90-F3: one shared writer (it vets against every deny before storing).
        rule_id = actions.learn_policy_rule(
            store,
            rule_id=f"network:fetch:{domain}",
            action="fetch",
            pattern=domain,
            scope="network",
            provenance=f"allow-always:{actor}",
        )
        return {"allowed_fetch_domain": domain, "rule_id": rule_id or f"network:fetch:{domain}"}
    if name == "allow_mcp_tool":
        from ..mcp_client import MCPTool, mcp_tool_scope_action

        server_id = str(args["server_id"])
        tool_name = str(args["tool"])
        # v41-F3: the rule lands in the scope the tool actually decides under
        # (email/read or email/send for a mail-bound server, else mcp/call).
        rule_scope, rule_action = mcp_tool_scope_action(
            store, MCPTool(server_id=server_id, name=tool_name, description="")
        )
        pattern = f"{server_id}:{tool_name}"
        # v90-F3: one shared writer (it vets against every deny before storing).
        rule_id = actions.learn_policy_rule(
            store,
            rule_id=f"{rule_scope}:{server_id}:{tool_name}",
            action=rule_action,
            pattern=pattern,
            scope=rule_scope,
            provenance=f"allow-always:{actor}",
        )
        return {
            "allowed": pattern,
            "rule_id": rule_id or f"{rule_scope}:{server_id}:{tool_name}",
            "scope": rule_scope,
        }
    if name == "revoke_policy_rule":
        # v109-F9: the narrowing half of allow_fetch_domain / allow_mcp_tool /
        # the session grants — one shared verb; the REST DELETE is the
        # operator-direct face of the same function (I5).
        return actions.revoke_policy_rule(store, rule_id=str(args["rule_id"]))
    if name == "set_policy":
        return actions.update_policy(store, holder, args)
    if name == "apply_policy_preset":
        allowed = actions.apply_shell_preset(store, holder, str(args["preset"]))
        return {"action": "preset_applied", "allowed_shell_commands": allowed}
    if name == "allow_env_bootstrap":
        return {
            "action": "env_bootstrap_allowed",
            "allowed_shell_commands": actions.allow_env_bootstrap(store, holder),
        }
    if name == "allow_shell_command":
        allowed = actions.allow_shell_command(store, holder, str(args["command"]))
        return {"action": "shell_command_allowed", "allowed_shell_commands": allowed}
    if name == "set_operator_policy":
        # v52-F4: the Queen's standing policy, edited behind the card the
        # operator just confirmed; run/worker policy documents are untouched.
        return actions.set_operator_policy_rule(
            store,
            scope=str(args["scope"]),
            action=str(args["action"]),
            pattern=str(args["pattern"]),
            verdict=str(args["verdict"]),
        )
    if name == "approve_review":
        review_id = str(args["review_id"])
        approval = actions.pending_approval_or_409(store, review_id)
        run = actions.require_run(store, str(approval["task_id"]))
        if run["state"] == "pending_approval":
            resumed = actions.resume_past_gate(store, holder.current, runner, run, review_id, actor)
            resumed_result: dict[str, Any] = {"action": "resumed", "resumed_as": resumed}
            # v109-F8: the Nth identical approval says so — the model relays
            # the nudge instead of silently re-approving forever.
            suggestion = actions.remember_suggestion_for_review(store, review_id)
            if suggestion is not None:
                resumed_result["suggestion"] = suggestion
            return resumed_result
        note = None if args.get("note") is None else str(args["note"])
        requested_branch = _queen_landing_branch(store, run, args.get("branch"))
        landed = actions.apply_patch(store, run, review_id, actor, note, branch=requested_branch)
        applied: dict[str, Any] = {"action": "applied", "branch": landed}
        # v20-F3: warn on landing when the supervisor could not re-verify the run.
        warning = actions.reverification_warning(store.reverification_for(str(run["task_id"])))
        if warning is not None:
            applied["warning"] = warning
        return applied
    if name == "land_run":
        run = actions.require_run(store, str(args["task_id"]))
        return actions.land_run(
            store,
            run,
            actor,
            note=None if args.get("note") is None else str(args["note"]),
            branch=_queen_landing_branch(store, run, args.get("branch")),
        )
    if name == "open_pr":
        # v47-F3: land (if needed) then PR — supervisor-side, on the operator's
        # own gh credentials; workers never touch git remotes.
        grouped_ids = args.get("task_ids")
        selectors = [key for key in ("task_id", "task_ids", "branch") if args.get(key)]
        if len(selectors) != 1:
            raise ValueError("pass exactly one of task_id, task_ids, or branch")
        if args.get("branch"):
            # v96-F4: an existing branch, pushed and PR'd as-is (no run).
            if not args.get("repo"):
                raise ValueError("branch mode needs repo= (registered repo slug or host path)")
            return actions.open_pr_for_branch(
                holder,
                store,
                str(args["repo"]),
                branch=str(args["branch"]),
                base=str(args.get("base") or "main"),
                title=None if args.get("title") is None else str(args["title"]),
            )
        if grouped_ids:
            # v54-F4: several related runs → one shared branch, one PR.
            if not isinstance(grouped_ids, list):
                raise ValueError("task_ids must be a list of task ids")
            grouped = [actions.require_run(store, str(task_id)) for task_id in grouped_ids]
            return actions.open_pr_for_runs(
                store,
                holder.current.audit_dir,
                grouped,
                actor,
                base=str(args.get("base") or "main"),
                note=None if args.get("note") is None else str(args["note"]),
                title=None if args.get("title") is None else str(args["title"]),
            )
        run = actions.require_run(store, str(args["task_id"]))
        return actions.open_pr_for_run(
            store,
            holder.current.audit_dir,
            run,
            actor,
            base=str(args.get("base") or "main"),
            note=None if args.get("note") is None else str(args["note"]),
        )
    if name == "merge_pr":
        # v47-F5: the only base-branch advance, strictly behind the confirm the
        # operator just gave. gh failure degrades to an honest tool result.
        from .. import github

        repo_path = resolve_repo_arg(str(args["repo"]), repos_root(holder), store)
        merge = github.merge_pull_request(
            repo=repo_path,
            pr=str(args["pr"]),
            strategy=str(args.get("strategy") or "merge"),
        )
        return {"merged": merge.merged, "detail": merge.detail}
    if name == "close_pr":
        # v58-F1: the un-merge verb. Reversible for the PR itself (a closed PR
        # reopens) — but NOT with delete_branch: deleting the head ref cascade-
        # closes every other PR built on it, upstream included, and the ref
        # does not come back (v106-F5 / v101-F16 — the card says so).
        from .. import github

        repo_path = resolve_repo_arg(str(args["repo"]), repos_root(holder), store)
        close = github.close_pull_request(
            repo=repo_path,
            pr=str(args["pr"]),
            delete_branch=bool(args.get("delete_branch") or False),
        )
        return {"closed": close.closed, "detail": close.detail}
    if name == "allow_command_review":
        review_id = str(args["review_id"])
        approval = actions.pending_approval_or_409(store, review_id)
        run = actions.require_run(store, str(approval["task_id"]))
        # v109-F7: one chat tool, routed by what is actually blocked — a
        # network approval remembers its host, everything else its command.
        if str(approval.get("action") or "") in ("network.fetch", "network.read"):
            resumed = actions.allow_network_host_and_resume(
                store, holder, runner, run, approval, review_id, actor
            )
            return {"action": "allowed_host", "resumed_as": resumed}
        resumed = actions.allow_shell_command_and_resume(
            store, holder, runner, run, approval, review_id, actor
        )
        return {"action": "allowed_command", "resumed_as": resumed}
    if name == "deny_review":
        review_id = str(args["review_id"])
        actions.pending_approval_or_409(store, review_id)
        note = None if args.get("note") is None else str(args["note"])
        store.resolve_approval(review_id, approved=False, actor=actor, note=note)
        return {"action": "denied"}
    if name == "register_repo":
        return register_repo(
            repos_root(holder),
            url=str(args["url"]),
            name=None if args.get("name") is None else str(args["name"]),
        )
    if name == "refresh_repo":
        return actions.refresh_repo(holder, str(args["repo"]), store=store)
    if name == "create_branch":
        return actions.create_branch(
            holder,
            str(args["repo"]),
            name=str(args["name"]),
            from_ref=None if args.get("from_ref") is None else str(args["from_ref"]),
            store=store,
        )
    if name == "delete_branch":
        return actions.delete_branch(
            holder,
            str(args["repo"]),
            name=str(args["name"]),
            remote=bool(args.get("remote") or False),
            store=store,
        )
    if name == "merge_branch":
        return actions.merge_branch(
            holder,
            str(args["repo"]),
            source=str(args["source"]),
            into=str(args["into"]),
            store=store,
        )
    if name == "push_branch":
        return actions.push_branch(holder, str(args["repo"]), name=str(args["name"]), store=store)
    if name == "sync_fleet":
        # v110-F2: args are IGNORED by construction — the verb runs only the
        # operator's terminal-set pin (I4); a model argument can never steer it.
        return actions.sync_fleet(store)
    if name == "push_baseline":
        return actions.push_baseline(
            holder,
            str(args["repo"]),
            base=None if args.get("base") is None else str(args["base"]),
            store=store,
        )
    if name == "unregister_repo":
        from .registry import remove_registered_repo

        return remove_registered_repo(store, repos_root(holder), str(args["name"]))
    if name == "copy_project_policy":
        return actions.copy_project_policy(
            store, src=str(args["src_project"]), dst=str(args["dst_project"])
        )
    if name == "set_policy_group":
        # v97-F3 (ADR 0048): create/update, or copy-on-write fork in ONE card.
        return actions.set_policy_group(
            store,
            name=str(args["name"]),
            policy=_object_arg(args, "policy"),
            fork_from=None if args.get("fork_from") is None else str(args["fork_from"]),
            repoint_project=(
                None if args.get("repoint_project") is None else str(args["repoint_project"])
            ),
        )
    if name == "delete_policy_group":
        return actions.delete_policy_group(store, name=str(args["name"]))
    if name == "attach_policy_group":
        return actions.attach_policy_group(
            store, project_id=str(args["project_id"]), name=str(args["name"])
        )
    if name == "detach_policy_group":
        return actions.detach_policy_group(
            store, project_id=str(args["project_id"]), name=str(args["name"])
        )
    if name == "set_project_phase":
        # v25-F1: command-deck only — the model never sees this tool.
        return set_project_phase(store, str(args["project_id"]), str(args["phase"]))
    if name == "workon":
        return actions.workon(
            holder,
            store,
            path=str(args["path"]),
            pack=str(args.get("pack") or "trusted_local_dev"),
            phase=str(args.get("phase") or "build"),
        )
    if name == "setup_project":
        template_names = args.get("template_names")
        overrides = _object_arg(args, "policy_overrides")
        if args.get("engine") is not None:
            # v95-F4: the CLI's --engine sugar (v94-F5) for the chat tool —
            # validated at setup so a typo refuses naming the choices (I9);
            # an explicit policy_overrides.coding_engine wins over the sugar.
            from ..engines import resolve_engine

            resolve_engine(str(args["engine"]))
            overrides.setdefault("coding_engine", str(args["engine"]))
        if args.get("groups"):
            # v97-F4: same sugar shape as engine= — known-name vetting happens
            # in setup_project_record (one point for chat/CLI/HTTP, I5).
            if not isinstance(args["groups"], list):
                raise ValueError("groups must be a list of policy group names")
            overrides.setdefault("policy_groups", [str(group) for group in args["groups"]])
        return setup_project_record(
            run_store=store,
            root=repos_root(holder),
            project_id=str(args["project_id"]),
            name=str(args["name"]),
            # v25-F1: the command deck sets up by pack; the pack owns strategy.
            strategy=None if args.get("strategy") is None else str(args["strategy"]),
            pack_name=None if args.get("pack") is None else str(args["pack"]),
            phase=str(args.get("phase") or "build"),
            repo_path=None if args.get("repo_path") is None else str(args["repo_path"]),
            repo_slug=None if args.get("repo_slug") is None else str(args["repo_slug"]),
            template_names=(
                []
                if template_names is None
                else [str(template_name) for template_name in template_names]
            ),
            policy_overrides=overrides,
            seed_default_schedules=bool(args.get("seed_default_schedules", True)),
        )
    if name == "dispatch_run":
        # v109-F6: computed BEFORE the submit so the hint names the PRIOR
        # run, never the one this dispatch creates — a hint, not a block.
        hint = actions.preserved_resumable_hint(
            holder,
            store,
            repo=str(args["repo"]),
            ref=None if args.get("ref") is None else str(args["ref"]),
        )
        task_id = actions.submit_run(
            holder,
            runner,
            store,
            repo=str(args["repo"]),
            instructions=str(args["instructions"]),
            caste=str(args.get("caste") or "coding"),
            execution_mode=(
                None if args.get("execution_mode") is None else str(args["execution_mode"])
            ),
            network=None if args.get("network") is None else [str(d) for d in args["network"]],
            wall_clock_seconds=_optional_int(args, "wall_clock_seconds"),
            max_iterations=_optional_int(args, "max_iterations"),
            max_actions=_optional_int(args, "max_actions"),
            max_provider_calls=_optional_int(args, "max_provider_calls"),
            ref=None if args.get("ref") is None else str(args["ref"]),
            dispatch_decision=decision,
            protocol=None if args.get("protocol") is None else str(args["protocol"]),
            engine=None if args.get("engine") is None else str(args["engine"]),
        )
        # v40-F2 (v35): repo + caste ride the result so both the live SSE tool
        # event and the stored transcript row can render a human summary —
        # additive only, same field set on both paths.
        result_view = {
            "task_id": task_id,
            "state": "dispatched",
            "repo": str(args["repo"]),
            "caste": str(args.get("caste") or "coding"),
        }
        if hint is not None:
            result_view["hint"] = hint
        return result_view
    if name == "start_research":
        from ..templates import deep_research_template, instantiate

        source_allowlist = [str(domain) for domain in args["source_allowlist"]]
        template = deep_research_template(source_allowlist)
        instance = instantiate(
            template,
            {
                "question": str(args["question"]),
                "depth": str(args.get("depth") or "standard"),
                "output_format": str(args.get("output_format") or "markdown"),
                # v46-F1: the discovered article URLs; the allowlist (not this
                # list) stays the egress boundary — off-list seeds are refused.
                "sources": " ".join(str(u) for u in args.get("seed_urls") or []),
            },
            repo=str(args["repo"]),
        )
        task_id = actions.submit_run(
            holder,
            runner,
            store,
            repo=str(args["repo"]),
            instructions=instance.instructions,
            caste="researcher",
            execution_mode="sandbox",  # research runs are sandboxed
            network=source_allowlist,
            dispatch_decision=decision,
        )
        return {"task_id": task_id, "state": "research_dispatched"}
    if name == "propose_schedule":
        # v41-F1: the chat/deck face of POST /api/schedules — same create
        # semantics (name reuse replaces; trust is enforced at tick time).
        from ..scheduler import (
            make_schedule,
            make_template_schedule,
            parse_interval,
            validate_chain,
        )

        interval = parse_interval(str(args["every"]))
        schedule_chain = None if args.get("chain") is None else str(args["chain"])
        validate_chain(store, name=str(args["name"]), chain=schedule_chain)
        if args.get("caste") in ("note", "script", "digest", "prompt"):
            # note/script/digest/prompt schedules are repo-less: the tick posts
            # the text (note), runs the command supervisor-side and posts its
            # output (script, v44-F4), composes a store summary (digest,
            # v47-F6), or runs a read-only Queen turn (prompt, v83-F5) into the
            # creating chat — or as an inert note when created outside one —
            # instead of dispatching a worker. A model-proposed schedule ALWAYS
            # rides the confirm card with the text verbatim in the args; there
            # is no auto-execute path.
            caste = str(args["caste"])
            if caste != "digest" and args.get("instructions") is None:
                raise ValueError(
                    f"a {caste} schedule needs instructions "
                    f"(the {'shell command' if caste == 'script' else 'text to run'})"
                )
            if caste == "prompt" and chat_id is None:
                raise ValueError(
                    "a prompt schedule runs its Queen turn in the chat that "
                    "created it — propose it from the chat that should "
                    "receive the recurring reply"
                )
            schedule = make_schedule(
                name=str(args["name"]),
                repo="",
                instructions=str(args.get("instructions") or ""),
                interval_seconds=interval,
                worker_kind=caste,
                chat_id=chat_id,
                once=bool(args.get("once") or False),
                start_at=None if args.get("start_at") is None else str(args["start_at"]),
                chain=schedule_chain,
            )
            store.add_schedule(schedule)
            return actions.schedule_view(store, schedule)
        if args.get("repo") is None:
            raise ValueError("a worker schedule needs a repo")
        repo_path = resolve_repo_arg(str(args["repo"]), repos_root(holder), store)
        if not (repo_path / ".git").exists():
            raise ValueError(f"{repo_path} is not a git repository")
        if args.get("template") is not None:
            schedule_template = store.get_template(str(args["template"]))
            if schedule_template is None:
                raise ValueError(f"no template named {str(args['template'])!r}")
            raw_params = _object_arg(args, "params")
            schedule = make_template_schedule(
                name=str(args["name"]),
                template=schedule_template,
                params={str(key): str(value) for key, value in raw_params.items()},
                repo=repo_path,
                interval_seconds=interval,
                chain=schedule_chain,
            )
        else:
            if args.get("instructions") is None:
                raise ValueError("a schedule needs instructions or a template")
            schedule = make_schedule(
                name=str(args["name"]),
                repo=repo_path,
                instructions=str(args["instructions"]),
                interval_seconds=interval,
                worker_kind=str(args.get("caste") or "coding"),
                once=bool(args.get("once") or False),
                start_at=None if args.get("start_at") is None else str(args["start_at"]),
                chain=schedule_chain,
            )
        store.add_schedule(schedule)
        return actions.schedule_view(store, schedule)
    if name == "delete_schedule":
        # v47-F1: CRUD honesty — the chat face of DELETE /api/schedules/{name}.
        if not store.remove_schedule(str(args["name"])):
            raise ValueError(f"no schedule named {str(args['name'])!r}")
        return {"removed": str(args["name"])}
    if name == "set_schedule_enabled":
        # v47-F1: the chat face of PATCH /api/schedules/{name}.
        if not store.set_schedule_enabled(str(args["name"]), enabled=bool(args["enabled"])):
            raise ValueError(f"no schedule named {str(args['name'])!r}")
        toggled = store.get_schedule(str(args["name"]))
        assert toggled is not None
        return actions.schedule_view(store, toggled)
    if name == "run_schedule_now":
        # v70-F5: run-now moves WHEN, never HOW — the schedule becomes due and
        # the ticker dispatches it on its next tick through the same policy/
        # delivery/health path as any scheduled run. No second dispatcher, and
        # the model still never holds the trigger.
        from ..scheduler import now_ts

        due_schedule = store.get_schedule(str(args["name"]))
        if due_schedule is None:
            raise ValueError(f"no schedule named {str(args['name'])!r}")
        if not due_schedule.enabled:
            raise ValueError(
                f"schedule {due_schedule.name!r} is disabled — set_schedule_enabled first"
            )
        store.mark_schedule_due(due_schedule.name, due_at=now_ts())
        stored_interval = store.get_setting(TICKER_INTERVAL_SECONDS)
        interval = (
            stored_interval
            if isinstance(stored_interval, int) and stored_interval >= 1
            else DEFAULT_TICKER_INTERVAL
        )
        return {"due": due_schedule.name, "runs_within_seconds": interval}
    if name == "set_run_completion_notify":
        # v47-F7: a store setting, carded like every mutation. OFF by default.
        from .run_status import NOTIFY_COMPLETION_SETTING

        enabled = bool(args["enabled"])
        store.set_setting(NOTIFY_COMPLETION_SETTING, enabled)
        return {"notify_run_completion": enabled}
    if name == "set_assistant_model":
        # v72-F1: the brain dial. Carded like every mutation. The API key is
        # deliberately NOT settable here — it stays on the 0600-file path
        # (Settings / PUT /api/llm/config), so a chat turn can never move a
        # secret. scope 'chat' touches one row; scope 'default' writes the
        # shared settings every chat AND default worker inherits.
        from .llm import (
            LLM_BASE_URL,
            LLM_DEFAULT_MODEL,
            LLM_PROTOCOL,
            _write_through_profile,
            refresh_model_ctx,
        )

        model = str(args["model"]).strip()
        if not model:
            raise ValueError("model must be non-empty")
        scope = str(args.get("scope") or "default")
        if scope == "chat":
            if chat_id is None:
                raise ValueError("scope 'chat' only makes sense inside a chat")
            override = None if model == "default" else model
            store.set_chat_model(chat_id, override)
            if override:
                # v74-F2: a chat pinned to a bigger model budgets like one.
                refresh_model_ctx(store, holder.current.home, override)
            return {"chat_model": override or "default"}
        protocol = args.get("protocol")
        if protocol is not None and protocol not in get_args(LLMProtocol):
            names = ", ".join(repr(value) for value in get_args(LLMProtocol))
            raise ValueError(f"protocol must be one of {names}")
        store.set_setting(LLM_DEFAULT_MODEL, model)
        base_url = args.get("base_url")
        if base_url is not None:
            store.set_setting(LLM_BASE_URL, str(base_url).strip().rstrip("/") or None)
        if protocol is not None:
            store.set_setting(LLM_PROTOCOL, protocol)
        refresh_model_ctx(store, holder.current.home, model)  # v74-F2
        _write_through_profile(store, holder.current.home.parent)
        return {
            "default_model": model,
            "note": "chats and default workers use this from their next turn",
        }
    if name == "add_provider":
        # v108-F2: same actions.py verb as `skep provider add` and
        # POST /api/providers. api_key_env carries a NAME, never a value.
        # v108-F3: preset fills the row from the catalog.
        def _opt(key: str) -> str | None:
            raw = args.get(key)
            return (str(raw).strip() or None) if raw else None

        result = actions.add_provider(
            store,
            provider_id=_opt("provider_id"),
            protocol=_opt("protocol"),
            base_url=_opt("base_url"),
            model=_opt("model"),
            api_key_env=_opt("api_key_env"),
            cost_class=_opt("cost_class"),
            preset=_opt("preset"),
        )
        if bool(args.get("activate")):
            saved_id = str(result["provider"]["provider_id"])
            result.update(actions.use_provider(store, holder.current.home, provider_id=saved_id))
        return result
    if name == "use_provider":
        return actions.use_provider(
            store, holder.current.home, provider_id=str(args["provider_id"]).strip()
        )
    if name == "remove_provider":
        return actions.remove_provider(store, provider_id=str(args["provider_id"]).strip())
    if name == "set_tts_provider":
        # v53-F6 (ADR 0031): config-gated channel infrastructure; the result
        # names the egress truth so the transcript records what was chosen.
        from ..voice import PROVIDER_EGRESS_NOTES, TTS_PROVIDER_SETTING, TTS_PROVIDERS

        provider = str(args["provider"])
        if provider not in TTS_PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(TTS_PROVIDERS)}")
        store.set_setting(TTS_PROVIDER_SETTING, provider)
        return {"tts_provider": provider, "egress": PROVIDER_EGRESS_NOTES[provider]}
    if name == "set_skill_observer":
        # v53-F1 (ADR 0029): opt-in ambient behavior, carded — the v47-F7
        # posture. OFF by default.
        from ..observe import OBSERVER_SETTING

        enabled = bool(args["enabled"])
        store.set_setting(OBSERVER_SETTING, enabled)
        return {"conversation_skill_observer": enabled}
    if name == "set_personality":
        # v44-F10: style only, scoped to the chat the card lives in.
        from .chat import validate_personality

        if chat_id is None:
            raise ValueError("set_personality only makes sense inside a chat")
        normalized = validate_personality(str(args["value"]))
        store.set_chat_personality(chat_id, normalized or None)
        return {"personality": normalized or "default"}
    if name == "set_persona":
        # v53-F4 (ADR 0028): profile-level identity, one capped file in the
        # personal home; 'default' clears. Oversize is a clean tool error.
        from .persona import write_persona

        return write_persona(holder.current.home, str(args.get("text") or ""))
    if name in ("discord_delete_message", "discord_timeout_member"):
        # v44-F5: moderation verbs. Reaching here means the OPERATOR confirmed
        # the card in the web UI (these classes are never channel-confirmable
        # and have no auto-execute path) — the REST call is the whole effect.
        from . import discord_admin
        from .channels import resolve_channel_secret

        channel_config = store.get_channel_config("discord")
        token = resolve_channel_secret(holder.current.home, "discord")
        if channel_config is None or not channel_config.enabled or token is None:
            raise ValueError("the discord channel is not enabled/configured (Settings → channels)")
        if name == "discord_delete_message":
            deleted = discord_admin.delete_message(
                token, str(args["channel_id"]), str(args["message_id"])
            )
            if not deleted:
                raise ValueError(
                    "discord rejected the delete (message gone, or the bot lacks "
                    "Manage Messages there)"
                )
            return {"ok": True, "deleted_message": str(args["message_id"])}
        minutes = int(args["minutes"])
        if not 1 <= minutes <= discord_admin.MAX_TIMEOUT_MINUTES:
            raise ValueError(f"minutes must be 1..{discord_admin.MAX_TIMEOUT_MINUTES}")
        timed_out = discord_admin.timeout_member(
            token, str(args["guild_id"]), str(args["user_id"]), minutes
        )
        if not timed_out:
            raise ValueError(
                "discord rejected the timeout (the bot lacks Moderate Members, or "
                "the target outranks it)"
            )
        return {"ok": True, "timed_out": str(args["user_id"]), "minutes": minutes}
    if name == "set_task_due":
        task_id = str(args["task_id"])
        current = store.get_task(task_id)
        if current is None:
            raise ValueError(f"no task {task_id!r}")
        task = store.update_task(
            task_id,
            title=current.title,
            status=current.status,
            due_at=str(args["due_at"]).strip() or None,
            actor=actor,
            action="due_set",
        )
        return {"task": None if task is None else asdict(task)}
    if name == "delete_note":
        note_id = str(args["note_id"])
        if not store.delete_note(note_id, actor=actor):
            raise ValueError(f"no note {note_id!r}")
        return {"removed": True}
    if name == "delete_task":
        task_id = str(args["task_id"])
        if not store.delete_task(task_id, actor=actor):
            raise ValueError(f"no task {task_id!r}")
        return {"removed": True}
    if name == "approve_memory_proposal":
        item = store.approve_memory_proposal(str(args["proposal_id"]), actor=actor)
        return {"approved": True, "memory_id": item.memory_id}
    if name == "reject_memory_proposal":
        proposal = store.reject_memory_proposal(
            str(args["proposal_id"]), actor=actor, reason=str(args["reason"])
        )
        return {"rejected": proposal.proposal_id}
    if name == "forget_memory":
        if not store.forget_memory_item(str(args["memory_id"]), actor=actor):
            raise ValueError(f"no active memory item {str(args['memory_id'])!r}")
        return {"removed": True}
    if name == "remember":
        # v83-F4: the Queen's write path into memory is a PROPOSAL — the
        # human gate on permanence (memory.py) is untouched; approval is
        # where injection begins.
        from ..memory import MEMORY_CLASSES

        durable_classes = sorted(MEMORY_CLASSES - {"observation"})
        memory_class = str(args.get("memory_class") or "durable_preference")
        if memory_class == "observation":
            raise ValueError(
                "observation is the harvester's lane (TTL-swept, no proposal) — "
                f"pick a durable class: {', '.join(durable_classes)}"
            )
        if memory_class not in MEMORY_CLASSES:
            raise ValueError(
                f"unknown memory class {memory_class!r} — pick one of: {', '.join(durable_classes)}"
            )
        content = str(args["content"]).strip()
        if not content:
            raise ValueError("remember needs non-empty content — state the fact plainly")
        proposal = store.create_memory_proposal(
            memory_class=memory_class,
            content=content,
            actor=actor,
            rationale=None if args.get("rationale") is None else str(args["rationale"]),
            project_id=None if args.get("project") is None else str(args["project"]),
        )
        return {
            "proposal_id": proposal.proposal_id,
            "state": proposal.state,
            "note": (
                "filed for review — nothing is injected until the user approves "
                "(list_memory_proposals shows the queue)"
            ),
        }
    raise ValueError(f"unknown mutating tool {name!r}")


def mutation_executes_in_turn(
    name: str, args: dict[str, Any], *, store: RunStore, holder: ConfigHolder
) -> bool:
    decision = mutation_execution_decision(name, args, store=store, holder=holder)
    return decision is not None and decision.allows_execution()


def mutation_execution_decision(
    name: str, args: dict[str, Any], *, store: RunStore, holder: ConfigHolder
) -> AutonomyDecision | None:
    if name == "call_mcp_tool":
        # v40-F10: the resolved mcp scope decides — allow runs inside the
        # turn, require_approval cards, deny refuses without a card.
        from ..mcp_client import MCPTool, mcp_scope_decision

        return mcp_scope_decision(
            store,
            MCPTool(
                server_id=str(args.get("server_id") or ""),
                name=str(args.get("tool") or ""),
                description="",
            ),
        )
    if name == "run_code":
        # v51-F3: run_code auto-resolves exactly where a plain dispatch_run
        # would — the project's own auto-dispatch posture decides, and the
        # script envelope (sandbox, deny-all egress, no landing path) is
        # strictly TIGHTER than what that posture already trusts.
        return actions.dispatch_run_decision(
            holder, store, repo=str(args.get("repo") or ""), caste="script"
        )
    if name == "quick_edit":
        # v83-F10: a quick_edit IS a coding dispatch — same posture, same
        # gate as dispatch_run on this repo.
        return actions.dispatch_run_decision(
            holder, store, repo=str(args.get("repo") or ""), caste="coding"
        )
    if name == "batch_dispatch":
        # v51-F5: auto-resolve only when EVERY member matches its project's
        # auto-dispatch policy; the first gated member names itself on the
        # card. Malformed batches card too — the honest error surfaces on
        # confirm through the same validation execution uses.
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > BATCH_DISPATCH_CAP:
            return None
        for index, entry in enumerate(raw_tasks):
            if not isinstance(entry, dict):
                return None
            member = actions.dispatch_run_decision(
                holder,
                store,
                repo=str(entry.get("repo") or ""),
                caste=str(entry.get("caste") or "coding"),
                execution_mode=(
                    None if entry.get("execution_mode") is None else str(entry["execution_mode"])
                ),
                # v98-F1: an explicit engine is an explicit run override, so
                # this member gates the batch (actions.py explicit_run_overrides).
                engine=(None if entry.get("engine") is None else str(entry["engine"])),
            )
            if not member.allows_execution():
                return AutonomyDecision(
                    verdict="require_approval",
                    reason="dispatch.require_approval.batch_member_gated",
                    detail=f"task {index + 1}/{len(raw_tasks)}: {member.reason}",
                    decided_by=member.decided_by,
                )
        return AutonomyDecision(
            verdict="allow",
            reason="dispatch.auto_allowed.batch_project_policy_match",
            detail=f"{len(raw_tasks)} task(s), each matching its project's auto-dispatch policy",
        )
    if name == "run_shell":
        # v83-F9: hard guards deny (ungrantable), repo cwd refuses without a
        # run_repo rule, a shell/run allow runs in-turn, default cards.
        return queen_shell_decision(
            store,
            holder,
            command=str(args.get("command") or ""),
            cwd=None if args.get("cwd") is None else str(args["cwd"]),
        )
    if name == "start_process":
        # v83-F8: the run_background action — a 'run' grant never covers a
        # daemon (review item 3).
        return queen_shell_decision(
            store,
            holder,
            command=str(args.get("command") or ""),
            cwd=None if args.get("cwd") is None else str(args["cwd"]),
            background=True,
        )
    if name == "stop_process":
        # v83-F8 (review item 3): stopping CARDS by default — the operator
        # may be mid-debug on that server. A standing run_background rule
        # covering the command that STARTED it auto-stops: the grant that
        # trusted the daemon to run manages its lifecycle too (I5 — the
        # same rule, not a second permission system).
        record = store.get_process(str(args.get("proc_id") or ""))
        if record is None:
            return None  # the honest not-found error surfaces on confirm
        stop_decision = queen_shell_decision(
            store, holder, command=record.command, cwd=None, background=True
        )
        if stop_decision is not None and stop_decision.allows_execution():
            return AutonomyDecision(
                verdict="allow",
                reason="shell.allow.process_lifecycle",
                detail=record.command,
                decided_by=stop_decision.decided_by,
            )
        return None
    if name == "remember":
        # v83-F4: a proposal is inert — nothing reaches a prompt until a
        # human approves it (memory.py: "the human gate is where permanence
        # begins"). The gate is the APPROVAL, not the filing, so the filing
        # runs in-turn; the born-resolved action row still records it (I13).
        return AutonomyDecision(
            verdict="allow",
            reason="memory.auto_allowed.proposal_inert",
            detail="files a pending_review proposal; injection requires approval",
        )
    if name in {"read_file", "search_files"}:
        # v51-F2: the filesystem scope decides — operator roots run inside
        # the turn, other paths card, an explicit deny refuses without a card.
        from .fileio import queen_filesystem_decision

        return queen_filesystem_decision(
            store, holder, action="read", path=str(args.get("path") or "")
        )
    if name == "read_url":
        # v72-F7: a standing network/fetch grant runs the read in-turn;
        # no grant keeps the per-URL card exactly as before. A malformed
        # URL cards too — the honest error surfaces on confirm.
        import urllib.parse

        host = urllib.parse.urlparse(str(args.get("url") or "")).hostname or ""
        if not host:
            return None
        return fetch_grant_decision(store, host)
    if name != "dispatch_run":
        return None
    return actions.dispatch_run_decision(
        holder,
        store,
        repo=str(args["repo"]),
        caste=str(args.get("caste") or "coding"),
        execution_mode=(
            None if args.get("execution_mode") is None else str(args["execution_mode"])
        ),
        network=None if args.get("network") is None else [str(d) for d in args["network"]],
        wall_clock_seconds=_optional_int(args, "wall_clock_seconds"),
        max_iterations=_optional_int(args, "max_iterations"),
        max_actions=_optional_int(args, "max_actions"),
        max_provider_calls=_optional_int(args, "max_provider_calls"),
        engine=None if args.get("engine") is None else str(args["engine"]),
    )
