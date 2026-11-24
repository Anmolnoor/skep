"""Phase 10 10.2: the one in-process ``/`` command registry.

Every ``/`` command any surface answers is declared here once — name, usage,
help, and the surfaces that may run it. The decks are views of this table:
``skep chat`` (``cli_chat.COMMANDS``), the messengers
(``channels/commands.COMMANDS``, their menus and ``/help``) and the web deck
(app.js reads ``web_deck`` over ``GET /api/commands``; its executors are
pinned against the web slice by test). A deck dispatches by asking ``resolve``
first; a name its surface does not carry is refused there, before any handler
or model runs.

A row that proposes a confirm card names the card's ``verb``. The desk's
confirm path asks ``check_card`` before it claims a card, so a card is
confirmed where it was asked. The desk's two surfaces share one serve token
— one operator — so that ``surface`` is a client hint, not authorization
(Phase 10 R1): usability, and a cancel is taken on either. Both checks only
ever refuse.

Who runs a command: on ``web`` and ``cli`` the operator (serve token / the
local terminal); on a messenger an allow-listed member — admission happens
upstream (``channels``), and a verdict (/approve, /deny) still goes through
the card's own channel gate, where only a channel admin confirms beyond the
low-risk classes (ADR 0067). The registry adds no allow-logic of its own and
no new path to a verb (I5): a desk mutation still proposes an operator card
(``chat_actions`` source ``operator``) and acts only on its confirm (I7).

Scope only narrows: a messenger keeps exactly its v130 deck, so /role,
/policy and every other desk verb stay refused there (I6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WEB = "web"
CLI = "cli"
CHANNELS = frozenset({"telegram", "discord", "slack"})
SURFACES = frozenset({WEB, CLI, *CHANNELS})
_DESK = frozenset({WEB, CLI})
_CLI = frozenset({CLI})
_ALL = SURFACES


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    help: str
    surfaces: frozenset[str]
    verb: str | None = None  # the mutation its confirm card proposes, if any


class CommandRefused(Exception):
    """The surface does not carry this command; ``str()`` says why and where."""


def _row(
    name: str, usage: str, help: str, *surfaces: frozenset[str], verb: str | None = None
) -> SlashCommand:
    return SlashCommand(name, usage, help, frozenset().union(*surfaces), verb)


# Order is the /help order on every surface.
REGISTRY: tuple[SlashCommand, ...] = (
    _row("help", "/help", "list the command deck", _ALL),
    _row("policy", "/policy [repo]", "effective policy for a repo (default: last used)", _DESK),
    _row("repos", "/repos", "registered repos", _DESK),
    _row("skills", "/skills", "learned-skill candidates and admitted skills", _DESK),
    _row("runs", "/runs [n]", "recent runs (/runs 10)", _ALL),
    _row("approvals", "/approvals", "the pending approval queue", _DESK),
    _row("state", "/state <repo>", "a repo's git state: branches, HEAD, recent commits", _DESK),
    _row(
        "setup",
        "/setup <repo> [--pack X] [--phase Y]",
        "bind a repo to a trusted project (confirm card)",
        _DESK,
        verb="setup_project",
    ),
    _row(
        "phase",
        "/phase <project-id> <bootstrap|build|maintain>",
        "move a project's trust phase (confirm card)",
        _DESK,
        verb="set_project_phase",
    ),
    _row(
        "unregister",
        "/unregister <project-id>",
        "unregister a project: skep forgets it, its folder stays (confirm card)",
        _DESK,
        verb="remove_project",
    ),
    _row(
        "land",
        "/land <task-id> [branch]",
        "land a completed run's patch (confirm card)",
        _DESK,
        verb="land_run",
    ),
    _row(
        "approve",
        "/approve <review-id|card-id> [branch]",
        "approve a pending review (confirm card), or a pending card by its id",
        _DESK,
        verb="approve_review",
    ),
    _row(
        "deny",
        "/deny <review-id|card-id>",
        "deny a pending review (confirm card), or a pending card by its id",
        _DESK,
        verb="deny_review",
    ),
    _row(
        "workon",
        "/workon <path> [--pack X] [--phase Y]",
        "make a local directory a first-class workspace — "
        "git baseline + trusted project (confirm card)",
        _DESK,
        verb="workon",
    ),
    _row(
        "schedule",
        "/schedule <name> <repo> <every> <instructions…>",
        "create a recurring schedule the ticker dispatches — every takes 30m/6h/1d (confirm card)",
        _DESK,
        verb="propose_schedule",
    ),
    _row(
        "persona",
        "/persona <text…|default>",
        "set the profile-level identity every chat starts with — "
        "identity only, never policy (confirm card)",
        _DESK,
        verb="set_persona",
    ),
    _row(
        "role",
        "/role <builder|operator|assistant|custom-role>",
        "switch this chat's role — prompt, voice and policy layer together; "
        "a role only tightens the baseline (confirm card)",
        _DESK,
        verb="set_role",
    ),
    _row(
        "personality",
        "/personality <concise|technical|friendly|custom:text|default>",
        "set this chat's reply style — style only, never policy (confirm card)",
        _DESK,
        verb="set_personality",
    ),
    _row(
        "btw",
        "/btw <question…>",
        "a read-only side question, even beside a pending card: no cards, no mutations",
        _ALL,
    ),
    _row(
        "trust",
        "/trust [private|untrusted]",
        "trust this chat again: clear its taint (both bits by default) — "
        "yours only, never the model's; outside content read later sets it again",
        _DESK,
    ),
    _row(
        "steer",
        "/steer <task-id> <text…>",
        "send a steering note into a RUNNING react run — "
        "input, never authority: resolves no card, approval, or gate",
        _DESK,
    ),
    _row(
        "resume",
        "/resume <task-id>",
        "continue a crashed/timed-out run from its checkpoint — model-free (confirm card)",
        _DESK,
        verb="resume_run",
    ),
    _row(
        "browser",
        "/browser",
        "register the built-in Playwright browser under the browse scope (confirm card)",
        _DESK,
        verb="setup_browser",
    ),
    _row("status", "/status", "what is running and what waits on you", _ALL),
    _row(
        "model",
        "/model [name] [--scope chat|default]",
        "show effective model for this chat; with name: propose set_assistant_model",
        _CLI,
        verb="set_assistant_model",
    ),
    _row(
        "stop",
        "/stop",
        "stop the turn running here (cards stay, runs keep running)",
        _CLI,
        CHANNELS,
    ),
    _row("exit", "/exit", "leave the REPL — bare exit/quit also work", _CLI),
    _row("replay", "/replay", "print the last 20 messages of this chat", _CLI),
    # The messenger deck (v130): a conversation's own sessions and verdicts.
    _row("new", "/new [title]", "start a fresh session here (/new trip planning)", CHANNELS),
    _row("sessions", "/sessions", "this conversation's earlier sessions", CHANNELS),
    _row(
        "resume",
        "/resume <n|chat-id>",
        "go back to a session from /sessions (/resume 2)",
        CHANNELS,
    ),
    _row("title", "/title [name]", "show or rename this session (/title trip planning)", CHANNELS),
    _row("approvals", "/approvals", "cards waiting in this conversation", CHANNELS),
    _row(
        "whoami",
        "/whoami",
        "who skep thinks you are here, and whether you are admin",
        CHANNELS,
    ),
    _row(
        "approve",
        "/approve [card-id]",
        "confirm the waiting card (/approve 1a2b3c4d when several wait)",
        CHANNELS,
    ),
    _row("deny", "/deny [card-id] [reason]", "deny the waiting card (/deny 1a2b3c4d)", CHANNELS),
    _row("compact", "/compact", "fold this chat's overflow into its digest now", CHANNELS),
    _row("usage", "/usage", "tokens skep spent: this chat and all, 5h and 7d", CHANNELS),
    _row("model", "/model", "which model answers here (change it at the desk)", CHANNELS),
)


def commands_for(surface: str) -> tuple[SlashCommand, ...]:
    """The commands ``surface`` carries, in /help order."""
    return tuple(row for row in REGISTRY if surface in row.surfaces)


def resolve(surface: str, name: str) -> SlashCommand:
    """The one authorization step every deck takes before it runs ``/name``:
    the row ``surface`` carries, or ``CommandRefused`` naming where it runs."""
    rows = [row for row in REGISTRY if row.name == name]
    for row in rows:
        if surface in row.surfaces:
            return row
    if not rows:
        raise CommandRefused(f"unknown command: /{name}")
    raise _elsewhere(surface, name, rows)


def check_card(surface: str, verb: str) -> None:
    """The confirm path's second look: ``CommandRefused`` when only commands
    ``surface`` does not carry propose ``verb``. A verb no command proposes (a
    button's, a server-raised card's) is not the registry's to judge."""
    rows = [row for row in REGISTRY if row.verb == verb]
    if rows and not any(surface in row.surfaces for row in rows):
        raise _elsewhere(surface, rows[0].name, rows)


def web_deck() -> dict[str, Any]:
    """What ``GET /api/commands`` serves the browser: the web rows, and the
    refusal every other surface's command gets on the web (read-only)."""
    refusals: dict[str, str] = {}
    for row in REGISTRY:
        try:
            resolve(WEB, row.name)
        except CommandRefused as exc:
            refusals[row.name] = str(exc)
    rows = commands_for(WEB)
    return {
        "surface": WEB,
        "commands": [{"name": row.name, "usage": row.usage, "help": row.help} for row in rows],
        "refusals": refusals,
    }


def _elsewhere(surface: str, name: str, rows: list[SlashCommand]) -> CommandRefused:
    where = sorted(set().union(*(row.surfaces for row in rows)))
    return CommandRefused(f"/{name} is not available on {surface}; it runs on {', '.join(where)}")
