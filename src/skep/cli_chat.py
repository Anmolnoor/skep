"""``skep chat`` — the terminal face over the serve API (v38).

The REPL is an operator surface with the serve token — the same trust class
as the web UI, not a channel. Every request goes through the daemon's
token-gated HTTP+SSE surface (``ChatEngine`` behind ``add_chat_routes``), so
it inherits every gate, actor attribution, and audit row unchanged. The REPL
process never opens ``RunStore`` (a documented single-writer design) and
never spawns a dispatcher: if the daemon is down it refuses and names the
next command.

macOS note: the stdlib ``readline`` may be libedit-backed there; history
works everywhere, but tab-completion needs both binding syntaxes
(``tab: complete`` and ``bind ^I rl_complete``).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .supervisor.serve.auth import TOKEN_FILE

DEFAULT_URL = "http://127.0.0.1:8765"
TOKEN_HEADER = "X-Skep-Token"
PROMPT = "you › "  # noqa: RUF001 - the deliberate Hermes-style prompt glyph
REPLAY_MESSAGES = 20
DAEMON_HINT = "skep chat needs the daemon — run: skep serve"


def _tty() -> bool:
    """One predicate for every TTY-only rendering choice (boxes, links, the
    status prompt) — NO_COLOR governs color, never structure."""
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    """ANSI-wrap ``text`` when stdout is a TTY and NO_COLOR is unset."""
    if not _tty() or os.environ.get("NO_COLOR"):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


DIM = "2"
RED = "31"
CYAN = "36"
GREEN = "32"
YELLOW = "33"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _pc(code: str, text: str) -> str:
    """Prompt-colored: ANSI-wrap for ``input()`` with ``\\001…\\002`` markers
    so readline measures width correctly (the escapes are invisible)."""
    if not _tty() or os.environ.get("NO_COLOR"):
        return text
    return f"\001\x1b[{code}m\002{text}\001\x1b[0m\002"


def _link(text: str, url: str) -> str:
    """OSC 8 hyperlink on a TTY; plain text everywhere else (escape bytes must
    never reach a pipe). NO_COLOR does not disable links — it governs color,
    and a link is not color."""
    if not _tty():
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def _box(lines: list[str]) -> None:
    """Dim-bordered card on a TTY; the same lines print unboxed off-TTY (the
    v50-F1 script contract, byte-identical). Width measures PLAIN text — ANSI
    escapes count toward ``len()`` but not toward terminal cells. Lines are
    never truncated (I6/I8): an over-wide line wraps and the box degrades,
    which is acceptable where silent truncation is not."""
    if not _tty():
        for line in lines:
            print(line)
        return
    width = max(len(_ANSI_RE.sub("", line)) for line in lines)
    print(_c(DIM, "┌" + "─" * (width + 2) + "┐"))
    for line in lines:
        pad = " " * (width - len(_ANSI_RE.sub("", line)))
        print(_c(DIM, "│ ") + line + pad + _c(DIM, " │"))
    print(_c(DIM, "└" + "─" * (width + 2) + "┘"))


class ServeApiError(Exception):
    """A non-2xx answer from the daemon, with the JSON ``detail`` if present."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


# The injected client seam is typed Any on purpose: production uses httpx.Client,
# but the ASGI test client (fastapi TestClient) is an httpx2.Client — two
# incompatible type worlds with one identical call surface.
HttpxClientLike = Any
HttpxResponseLike = Any


def _error_detail(response: HttpxResponseLike) -> str:
    try:
        detail = response.json().get("detail", "")
    except (json.JSONDecodeError, ValueError):
        detail = response.text
    return str(detail) or str(response.status_code)


class ServeClient:
    """Thin httpx wrapper for the daemon's /api surface (header-token auth).

    Tests inject an ASGI-backed ``httpx.Client`` (the FastAPI ``TestClient``);
    the real REPL builds one against ``--url`` / ``SKEP_SERVE_URL`` /
    ``http://127.0.0.1:8765``.
    """

    def __init__(self, base_url: str, token: str, *, client: HttpxClientLike | None = None) -> None:
        self._headers = {TOKEN_HEADER: token}
        self._client = (
            client
            if client is not None
            # read=None: SSE turn streams stay open as long as the model talks.
            else httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, read=None))
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(path, headers=self._headers, params=params)
        return self._checked(response)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self._client.post(path, headers=self._headers, json=body or {})
        return self._checked(response)

    @contextmanager
    def stream(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Iterator[HttpxResponseLike]:
        kwargs: dict[str, Any] = {"headers": self._headers}
        if body is not None:
            kwargs["json"] = body
        with self._client.stream(method, path, **kwargs) as response:
            if response.status_code >= 400:
                response.read()
                raise ServeApiError(response.status_code, _error_detail(response))
            yield response

    @staticmethod
    def _checked(response: HttpxResponseLike) -> Any:
        if response.status_code >= 400:
            raise ServeApiError(response.status_code, _error_detail(response))
        return response.json() if response.content else {}


def iter_sse(response: HttpxResponseLike) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse the SSE stream ``_sse()`` writes — the browser ``streamSse`` contract.

    Blocks split on the blank line; ``event:``/``data:`` lines inside; the
    default event name is ``message`` (assistant content deltas).
    """
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            event = "message"
            data: dict[str, Any] | None = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: ") :])
            if data is not None:
                yield event, data


# ---------------------------------------------------------------------------
# The command deck (v25 posture, third rendering): deterministic operator
# /commands the model never sees. Reads hit REST directly; mutations become
# operator-sourced chat_actions resolved under actor 'operator-command'.
# Keep COMMANDS, the executor branches in ChatRepl.run_command, and /help in
# lockstep — tests pin table == executor AND table == the app.js deck.
# ---------------------------------------------------------------------------

COMMANDS: dict[str, dict[str, str]] = {
    "help": {"usage": "/help", "desc": "list the command deck"},
    "policy": {
        "usage": "/policy [repo]",
        "desc": "effective policy for a repo (default: last used)",
    },
    "repos": {"usage": "/repos", "desc": "registered repos"},
    "skills": {"usage": "/skills", "desc": "learned-skill candidates and admitted skills"},
    "runs": {"usage": "/runs [n]", "desc": "recent runs"},
    "approvals": {"usage": "/approvals", "desc": "the pending approval queue"},
    "state": {
        "usage": "/state <repo>",
        "desc": "a repo's git state: branches, HEAD, recent commits",
    },
    "setup": {
        "usage": "/setup <repo> [--pack X] [--phase Y]",
        "desc": "bind a repo to a trusted project (confirm card)",
    },
    "phase": {
        "usage": "/phase <project-id> <bootstrap|build|maintain>",
        "desc": "move a project's trust phase (confirm card)",
    },
    "land": {
        "usage": "/land <task-id> [branch]",
        "desc": "land a completed run's patch (confirm card)",
    },
    "approve": {
        "usage": "/approve <review-id|card-id> [branch]",
        "desc": "approve a pending review (confirm card), or a pending card by its id",
    },
    "deny": {
        "usage": "/deny <review-id|card-id>",
        "desc": "deny a pending review (confirm card), or a pending card by its id",
    },
    "workon": {
        "usage": "/workon <path> [--pack X] [--phase Y]",
        "desc": "make a local directory a first-class workspace — "
        "git baseline + trusted project (confirm card)",
    },
    "schedule": {
        "usage": "/schedule <name> <repo> <every> <instructions…>",
        "desc": "create a recurring schedule the ticker dispatches — "
        "every takes 30m/6h/1d (confirm card)",
    },
    "persona": {
        "usage": "/persona <text…|default>",
        "desc": "set the profile-level identity every chat starts with — "
        "identity only, never policy (confirm card)",
    },
    "personality": {
        "usage": "/personality <concise|technical|friendly|custom:text|default>",
        "desc": "set this chat's reply style — style only, never policy (confirm card)",
    },
    "btw": {
        "usage": "/btw <question…>",
        "desc": "ask a side question WITHOUT touching running work — "
        "read-only turn: no cards, no mutations, runs beside a pending card",
    },
    "steer": {
        "usage": "/steer <task-id> <text…>",
        "desc": "send a steering note into a RUNNING react run — "
        "input, never authority: resolves no card, approval, or gate",
    },
    "resume": {
        "usage": "/resume <task-id>",
        "desc": "continue a crashed/timed-out run from its checkpoint — model-free (confirm card)",
    },
    "browser": {
        "usage": "/browser",
        "desc": "register the built-in Playwright browser under the browse scope (confirm card)",
    },
    "status": {
        "usage": "/status",
        "desc": "re-print the startup banner: assistant readiness, approvals/cards waiting",
    },
    "model": {
        "usage": "/model [name] [--scope chat|default]",
        "desc": "show effective model for this chat; with name: propose set_assistant_model",
    },
    "exit": {
        "usage": "/exit",
        "desc": "leave the REPL — bare exit/quit also work",
    },
    "replay": {
        "usage": "/replay",
        "desc": f"print the last {REPLAY_MESSAGES} messages of this chat",
    },
}

# CLI_ONLY: commands that exist ONLY in the terminal — the web has equivalent
# surfaces but no deck rows (v77-F3 parity pin): /status -> web status page +
# topbar health dot; /model -> composer model select; /exit -> close tab;
# /replay -> the web transcript is always on screen (v77-F5).
CLI_ONLY = frozenset({"status", "model", "exit", "replay"})


def _parse_slash(text: str) -> tuple[str, list[str], dict[str, str]]:
    """``/name arg --flag value`` → (name, args, flags) — parseSlashCommand's twin."""
    tokens = text.strip().split()
    name = tokens[0][1:].lower()
    args: list[str] = []
    flags: dict[str, str] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and index + 1 < len(tokens):
            flags[token[2:]] = tokens[index + 1]
            index += 2
        else:
            args.append(token)
            index += 1
    return name, args, flags


def _project_id_for_repo(repo: str) -> str:
    basename = repo.rstrip("/").split("/")[-1] or repo
    return re.sub(r"[^a-z0-9._-]+", "-", basename.lower()).strip("-.")


def _repo_binding(repo: str) -> dict[str, str]:
    """A registered slug binds by slug; anything path-like binds by host path."""
    return {"repo_path": repo} if re.search(r"[/~]", repo) else {"repo_slug": repo}


def _verify_pin_note(preview: dict[str, Any]) -> str:
    """v91-F1 (I8): the setup card says which command G10 will re-run — the
    project's pin, or the weaker "whatever the worker nominates" fallback."""
    policy = preview.get("effective_policy")
    command = str((policy or {}).get("verify_command") or "").strip()
    if command:
        return f"verify_command: {command}"
    return "no verify_command — G10 re-runs the worker's own verify step"


def _print_help() -> None:
    width = max(len(spec["usage"]) for spec in COMMANDS.values())
    for spec in COMMANDS.values():
        print(f"  {spec['usage']:<{width}}  {spec['desc']}")


def _enable_completion() -> None:
    """Tab-complete deck command names — and nothing deeper."""
    try:
        import readline
    except ImportError:  # pragma: no cover - non-readline platforms
        return
    names = ["/" + name for name in COMMANDS]

    def complete(text: str, state: int) -> str | None:
        matches = [name for name in names if name.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n")
    # macOS ships a libedit-backed readline with its own binding spelling.
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def _card_choice() -> str:
    """The house single-key idiom (termios on a TTY, input() fallback).

    Resolved through the cli_cmds module attribute so tests monkeypatch one
    place; EOF/non-answers come back as ``s`` — skip, never act.
    """
    from .supervisor import cli_cmds

    return cli_cmds._read_approval_choice()


def _render_card(action: dict[str, Any]) -> None:
    """One confirmation card — tool, args, decision — boxed on a TTY (v77-F1:
    the single most consequential prompt carries visual weight, I6)."""
    lines = [_c(CYAN, f"confirm: {action.get('tool', '?')}")]
    args = action.get("args") or {}
    if args:
        lines.extend(
            "  " + line for line in json.dumps(args, indent=2, ensure_ascii=True).splitlines()
        )
    decision = action.get("decision")
    if isinstance(decision, dict):
        parts = [str(decision.get("verdict") or ""), str(decision.get("reason") or "")]
        detail = decision.get("detail")
        if detail:
            parts.append(f"({detail})")
        lines.append(_c(DIM, "  " + " ".join(part for part in parts if part)))
    _box(lines)


def _tool_summary(tool: str, result: Any, url: str = "") -> str:
    """One deterministic line per tool result — read off the result JSON only,
    never model text (the v40-F2 posture). Live SSE events and replayed store
    rows read the same fields, so both faces render the same shapes.
    # ponytail: generic on purpose — per-tool phrasing (the web's ~40-branch
    # toolLine) gets ported only if the field asks; a duplicated table across
    # faces is drift waiting for a lockstep test that doesn't exist yet.
    """
    if isinstance(result, dict):
        if result.get("error"):
            return f"✗ {result['error']}"
        if result.get("ok") is False:
            return f"✗ {result.get('error') or 'refused'}"
        if result.get("ok") is True and isinstance(result.get("result"), dict):
            # Mutation payloads ride an {ok, result} wrapper — summarize the meat.
            return _tool_summary(tool, result["result"], url)
        lists = [(key, value) for key, value in result.items() if isinstance(value, list)]
        if len(lists) == 1:
            key, value = lists[0]
            return f"✓ {len(value)} {key}"
        task_id = result.get("resumed_as") or result.get("task_id")
        if task_id:
            # v77-F5: the id is clickable where the terminal supports OSC 8.
            tid = str(task_id)
            return f"✓ run {_link(tid, f'{url}/#/runs/{tid}') if url else tid}"
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=True)
    return f"✓ ok ({len(text)} chars)"


class _TurnPrinter:
    """Render one SSE turn to stdout, delta by delta (the Hermes feel)."""

    def __init__(self, *, show_thinking: bool, url: str = "") -> None:
        self.show_thinking = show_thinking
        self.url = url  # v77-F5: task ids in tool summaries link to their run page
        self.state: str | None = None
        self.actions: list[dict[str, Any]] = []
        self._midline = False

    def _break_line(self) -> None:
        if self._midline:
            sys.stdout.write("\n")
            self._midline = False

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._midline = not text.endswith("\n")

    def feed(self, event: str, data: dict[str, Any]) -> None:
        if event == "message":
            content = str(data.get("content") or "")
            if content:
                self._write(content)
        elif event == "thinking":
            if self.show_thinking:
                self._write(_c(DIM, str(data.get("thinking") or "")))
        elif event == "tool":
            # v77-F4: the thread shape — name, then an honest result line.
            # Consecutive tool events stack into a visually indented thread;
            # the full result stays on the web chat and the store transcript.
            self._break_line()
            name = str(data.get("tool", "?"))
            self._write("  " + _c(CYAN, f"▸ {name}") + "\n")
            self._write("    " + _c(DIM, _tool_summary(name, data.get("result"), self.url)) + "\n")
            # v90-F3: `decision` rides only a grant-covered mutation. Say so
            # here too — an unremarked auto-run recreates exactly the silence
            # the receipt exists to end (I8).
            decision = data.get("decision") or {}
            if decision:
                covered = decision.get("detail") or decision.get("reason") or ""
                note = (
                    f"ran without asking — covered by {covered}"
                    if covered
                    else ("ran without asking — a standing grant covers it")
                )
                self._write("    " + _c(DIM, note) + "\n")
                risk = (data.get("card") or {}).get("risk")
                if risk:
                    self._write("    " + _c(DIM, f"risk: {risk}") + "\n")
        elif event == "turn_status":
            # v87-F7: the wait names itself. The terminal shows a dim one-line
            # marker only for tool waits (await_runs can block for minutes);
            # per-round "thinking" markers would be noise between deltas.
            tool = str(data.get("tool") or "")
            if tool:
                self._break_line()
                self._write("  " + _c(DIM, f"… running {tool}") + "\n")
        elif event == "action":
            self._break_line()
            self.actions.append(data)
        elif event == "error":
            self._break_line()
            self._write(_c(RED, f"error: {data.get('detail', '')}") + "\n")
        elif event == "done":
            self._break_line()
            self.state = str(data.get("state") or "")

    def finish(self) -> None:
        self._break_line()


def render_turn(
    events: Iterator[tuple[str, dict[str, Any]]],
    *,
    show_thinking: bool = False,
    url: str = "",
) -> tuple[str | None, list[dict[str, Any]]]:
    """Print a turn's events; return (terminal state, pending action cards)."""
    printer = _TurnPrinter(show_thinking=show_thinking, url=url)
    for event, data in events:
        printer.feed(event, data)
    printer.finish()
    return printer.state, printer.actions


def _event_line(view: dict[str, Any]) -> str:
    """One line per event view from /api/runs/{id}/events (the _print_event
    style, minus the worktree reads that renderer needs)."""
    event_type = str(view.get("type") or "")
    payload = view.get("payload") or {}
    if event_type == "heartbeat":
        return f"… {payload.get('phase', 'working')}"
    if event_type == "task.start":
        return f"worker started (v{payload.get('worker_version', '?')})"
    if event_type == "plan.created":
        steps = payload.get("steps") or ["(no steps)"]
        return f"plan: {steps[0]}"
    if event_type == "command.start":
        return f"run: {payload.get('command', '')}"
    if event_type == "command.result":
        return f"exit {payload.get('exit_code')}: {payload.get('command', '')}"
    if event_type == "verify.result":
        return f"verification: {payload.get('outcome')}"
    if event_type == "approval.requested":
        reason = str(payload.get("reason") or "")
        return f"approval needed: {payload.get('action')}  {reason}".rstrip()
    if event_type == "approval.resolved":
        actor = str(payload.get("actor") or "")
        suffix = f" by {actor}" if actor else ""
        return f"approval resolved: {payload.get('action')} {payload.get('status')}{suffix}"
    if event_type == "reverify.result":
        return f"re-verify: {payload.get('outcome')}"
    if event_type == "task.terminal":
        summary = str(payload.get("summary") or "")
        return f"terminal: {payload.get('status')}  {summary}".rstrip()
    return event_type or "event"


def _print_command_result(result: dict[str, Any]) -> None:
    """Render a resolved deck command: ok → its result JSON, refused → red."""
    if result.get("ok"):
        payload = result.get("result")
        text = json.dumps(payload, indent=2, ensure_ascii=True) if payload is not None else "ok"
        print(text)
    elif result.get("denied"):
        print(_c(DIM, "canceled"))
    else:
        print(_c(RED, f"error: {result.get('error', 'refused')}"))


class ChatRepl:
    """The REPL session: one chat, one client, a loop of turns."""

    def __init__(
        self,
        client: ServeClient,
        chat: dict[str, Any],
        *,
        show_thinking: bool,
        url: str = DEFAULT_URL,
    ) -> None:
        self.client = client
        self.chat = chat
        self.chat_id = str(chat["chat_id"])
        self.show_thinking = show_thinking
        self.url = url  # v50-F3: the skipped-card hint names a real address
        self.last_repo: str | None = None  # /policy without an arg reuses it
        self.quit = False  # /exit sets it; loop() returns
        # v81-F12: oneshot --yes pre-confirms this invocation's cards.
        self.auto_confirm = False
        # v77-F2 refresh discipline: the status prompt fetches once per model
        # turn, not once per keystroke — deck reads change no context.
        self._prompt_cache: str | None = None

    def send(self, text: str) -> None:
        try:
            actions = self._post_message(text)
        except ServeApiError as exc:
            # The composer 409: cards pending. Resolve them at the prompt the
            # way the web composer forces the click, then retry the message once.
            if exc.status != 409 or "confirmation card" not in exc.detail:
                raise
            detail = self.client.get(f"/api/chats/{self.chat_id}")
            self.resolve_cards([a for a in detail["actions"] if a.get("status") == "proposed"])
            actions = self._post_message(text)
        self.resolve_cards(actions)
        self._prompt_cache = None  # a model turn moved the context meter

    def _post_message(self, text: str) -> list[dict[str, Any]]:
        with self.client.stream(
            "POST", f"/api/chats/{self.chat_id}/messages", {"content": text}
        ) as response:
            _state, actions = render_turn(
                iter_sse(response), show_thinking=self.show_thinking, url=self.url
            )
        return actions

    def resolve_cards(self, actions: list[dict[str, Any]]) -> None:
        for action in actions:
            self._resolve_card(action)

    def _resolve_card(self, action: dict[str, Any]) -> None:
        """One card, the house prompt: confirm and stream the continuation,
        deny likewise, anything else leaves it pending (the web UI still can)."""
        action_id = str(action.get("action_id"))
        _render_card(action)
        if self.auto_confirm:
            # v81-F12: the operator consented up front (--yes); no prompt.
            print(_c(DIM, f"--yes: confirming card {action_id}"))
            self._apply_card_verb(action, "confirm")
            return
        print("  [y] confirm  [n] deny  [s] skip (leave pending)")
        choice = _card_choice()
        if choice in {"y", "yes", "confirm"}:
            verb = "confirm"
        elif choice in {"n", "no", "d", "deny"}:
            verb = "deny"
        else:
            print(
                _c(
                    DIM,
                    f"card left pending ({action_id}) — /approve {action_id}, "
                    f"confirm at {self.url} or: skep chat --continue",
                )
            )
            return
        self._apply_card_verb(action, verb)

    def _apply_card_verb(self, action: dict[str, Any], verb: str) -> None:
        """Resolve one card on the right endpoint family (operator vs model)."""
        self._prompt_cache = None  # a verdict can change context or model
        action_id = str(action.get("action_id"))
        # v63-F1: a card found by cross-chat lookup resolves on ITS chat.
        chat_id = str(action.get("chat_id") or self.chat_id)
        if action.get("source") == "operator":
            # Deck commands resolve on the commands endpoints — JSON, no stream.
            result = self.client.post(f"/api/chats/{chat_id}/commands/{action_id}/{verb}")
            _print_command_result(result)
            if result.get("ok"):
                self._maybe_tail(result.get("result"))
            return
        with self.client.stream(
            "POST", f"/api/chats/{chat_id}/actions/{action_id}/{verb}"
        ) as response:
            _state, more = render_turn(
                iter_sse(response), show_thinking=self.show_thinking, url=self.url
            )
        if verb == "confirm":
            self._tail_after_action(action_id, chat_id=chat_id)
        self.resolve_cards(more)

    def _pending_card(self, action_id: str) -> dict[str, Any] | None:
        """The proposed action row matching ``action_id``, tagged with its chat.

        v51-F0: the pending-card hint (v50-F3) prints an action id no deck
        command accepted — /approve and /deny take it now. v63-F1: current
        chat first, then every other chat — flagless ``--oneshot`` mints a
        fresh chat per invocation, so the id the hint printed always lives in
        ANOTHER chat there; an exact id is unambiguous wherever it sits.
        """
        row = self._pending_card_in(self.chat_id, action_id)
        if row is not None:
            return row
        for chat in self.client.get("/api/chats")["chats"]:
            other_id = str(chat["chat_id"])
            if other_id == self.chat_id:
                continue
            row = self._pending_card_in(other_id, action_id)
            if row is not None:
                return row
        return None

    def _pending_card_in(self, chat_id: str, action_id: str) -> dict[str, Any] | None:
        detail = self.client.get(f"/api/chats/{chat_id}")
        row = next(
            (
                dict(a)
                for a in detail["actions"]
                if str(a.get("action_id")) == action_id and a.get("status") == "proposed"
            ),
            None,
        )
        if row is not None:
            row["chat_id"] = chat_id
        return row

    # -- run telemetry inline (F4): dispatched work tails at the prompt.

    def _tail_after_action(self, action_id: str, *, chat_id: str | None = None) -> None:
        """A confirmed card that dispatched a run auto-tails it. The tool
        result never rides the verdict stream, so read the resolved row."""
        detail = self.client.get(f"/api/chats/{chat_id or self.chat_id}")
        row = next((a for a in detail["actions"] if a.get("action_id") == action_id), None)
        if row is None or row.get("status") != "confirmed":
            return
        result = row.get("result")
        if isinstance(result, dict) and result.get("ok"):
            self._maybe_tail(result.get("result"))

    def _maybe_tail(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        task_id = payload.get("resumed_as") or payload.get("task_id")
        if task_id:
            self.tail_run(str(task_id))

    def _task_ref(self, task_id: str) -> str:
        """The FULL id, clickable where the terminal supports OSC 8 — full,
        not shortened: operators copy ids into /land, /steer, /resume, and a
        shortened display would break the copy (v77-F5)."""
        return _link(task_id, f"{self.url}/#/runs/{task_id}")

    def tail_run(self, task_id: str) -> None:
        """Stream a run's events inline. Ctrl-C stops WATCHING, never the run."""
        print(
            _c(
                DIM,
                f"run {self._task_ref(task_id)} — watching (Ctrl-C stops watching, not the run)",
            )
        )
        state: str | None = None
        try:
            with self.client.stream("GET", f"/api/runs/{task_id}/events?stream=1") as response:
                for event, data in iter_sse(response):
                    if event == "done":
                        state = str(data.get("state") or "")
                        break
                    print(f"  → {_event_line(data)}")
        except KeyboardInterrupt:
            print(_c(DIM, "stopped watching — the run continues; /runs finds it again"))
            return
        print(_c(DIM, f"run {self._task_ref(task_id)}: {state or 'gone'}"))
        if state == "pending_approval":
            self._approval_gate(task_id)

    def _approval_gate(self, task_id: str) -> None:
        """The run stopped for a human: render the house approval prompt over
        the HTTP verbs. Approving resumes into a successor run — tail it too."""
        # Pure predicate over the HTTP-served fields — not a store helper.
        from .supervisor.cli_cmds import _approval_can_be_remembered

        pending = [
            a for a in self.client.get("/api/approvals")["approvals"] if a.get("task_id") == task_id
        ]
        for approval in pending:
            action = str(approval.get("action") or "")
            reason = str(approval.get("reason") or "")
            review_id = str(approval.get("review_id"))
            lines = [_c(CYAN, f"approval needed: {action}")]
            if reason:
                lines.append(f"  reason: {reason}")
            _box(lines)  # v77-F1: same card concept, same weight
            # Only shell.run has a remember verb over HTTP (/allow-command).
            rememberable = action == "shell.run" and _approval_can_be_remembered(action, reason)
            if rememberable:
                print("  [a] approve once  [b] approve + remember  [d] deny  [s] skip")
            else:
                print("  [a] approve once  [d] deny  [s] skip")
            choice = _card_choice()
            actor = getpass.getuser()
            if choice in {"a", "approve", "y", "yes"}:
                result = self.client.post(f"/api/approvals/{review_id}/approve", {"actor": actor})
            elif rememberable and choice in {"b", "remember"}:
                result = self.client.post(
                    f"/api/approvals/{review_id}/allow-command", {"actor": actor}
                )
            elif choice in {"d", "deny", "n", "no"}:
                self.client.post(f"/api/approvals/{review_id}/deny", {"actor": actor})
                print(_c(DIM, f"denied: task {task_id}"))
                continue
            else:
                print(_c(DIM, "approval left pending"))
                continue
            resumed = result.get("resumed_as")
            if resumed:
                print(_c(DIM, f"resuming as {resumed}"))
                self.tail_run(str(resumed))
            elif result.get("branch"):
                print(f"applied on {result['branch']}")

    # -- the deck executor: one branch per COMMANDS entry (drift-pinned).

    def run_command(self, text: str) -> None:
        name, args, flags = _parse_slash(text)
        spec = COMMANDS.get(name)
        if spec is None:
            print(_c(RED, f"unknown command: /{name}") + " — /help lists the deck")
            return
        usage = f"usage: {spec['usage']}"
        if name == "help":
            _print_help()
            return
        if name == "repos":
            repos = self.client.get("/api/repos")["repos"]
            if not repos:
                print(_c(DIM, "(no registered repos)"))
            for repo in repos:
                print(f"{repo.get('name', '?')}  {repo.get('path', '')}")
            return
        if name == "skills":
            skills = self.client.get("/api/skills")["skills"]
            if not skills:
                print(_c(DIM, "(no skill candidates — `skep skill propose` drafts some)"))
            for skill in skills:
                print(f"{skill.get('name', '?')}  {skill.get('status', '?')}")
            return
        if name == "runs":
            limit = max(1, int(args[0]) if args and args[0].isdigit() else 10)
            runs = self.client.get(f"/api/runs?limit={limit}")["runs"]
            if not runs:
                print(_c(DIM, "(no runs yet)"))
            for run in runs:
                summary = str(run.get("summary") or "")[:60]
                task_ref = self._task_ref(str(run.get("task_id", "?")))
                print(f"{task_ref}  {run.get('state', '?'):<18}  {summary}")
            return
        if name == "approvals":
            approvals = self.client.get("/api/approvals")["approvals"]
            if not approvals:
                print(_c(DIM, "(no pending approvals)"))
            for approval in approvals:
                task_ref = self._task_ref(str(approval.get("task_id", "?")))
                print(
                    f"{approval.get('review_id', '?')}  {approval.get('action', '?')}  "
                    f"task {task_ref}  {approval.get('reason', '')}"
                )
            return
        if name in {"policy", "state"}:
            repo = args[0] if args else (self.last_repo if name == "policy" else None)
            if not repo:
                print(usage)
                return
            self.last_repo = repo
            tail = "effective-policy" if name == "policy" else "state"
            view = self.client.get(f"/api/repos/{quote(repo, safe='')}/{tail}")
            print(json.dumps(view, indent=2, ensure_ascii=True))
            return
        if name == "phase":
            if len(args) < 2:
                print(usage)
                return
            self._propose_command("set_project_phase", {"project_id": args[0], "phase": args[1]})
            return
        if name == "land":
            if not args:
                print(usage)
                return
            body: dict[str, Any] = {"task_id": args[0]}
            if len(args) > 1:
                body["branch"] = args[1]
            self._propose_command("land_run", body)
            return
        if name == "steer":
            if len(args) < 2:
                print(usage)
                return
            # v69-F4: typed text is the input — direct POST, no card; the
            # endpoint's 409s teach when steering cannot land.
            result = self.client.post(
                f"/api/runs/{quote(args[0], safe='')}/steer",
                {"text": " ".join(args[1:])},
            )
            print(_c(DIM, f"steered: {self._task_ref(str(result.get('task_id', args[0])))}"))
            return
        if name == "resume":
            if not args:
                print(usage)
                return
            # v73-F2: model-free crash recovery through the same carded verb.
            self._propose_command("resume_run", {"task_id": args[0]})
            return
        if name == "browser":
            # v83-F11: one card registers the Playwright MCP server.
            self._propose_command("setup_browser", {})
            return
        if name == "status":
            # v77-F3: re-print the banner on demand — "what needs me?" mid-session.
            _print_banner(self.client)
            print(_c(DIM, f"daemon: {self.url}"))
            return
        if name == "model":
            # v77-F3: show effective model (chat override or default); with name:
            # propose set_assistant_model. Scope: chat (default) or default.
            if not args:
                detail = self.client.get(f"/api/chats/{self.chat_id}")
                model = (detail.get("chat") or {}).get("model")
                ctx = detail.get("context", {})
                llm = self.client.get("/api/llm/config")
                if model:
                    print(f"this chat: {model} (override)")
                else:
                    print(f"default: {llm.get('default_model', 'unknown')}")
                pct = ctx.get("percent", 0)
                win = ctx.get("window_tokens", 0)
                src = ctx.get("num_ctx_source", "unknown")
                print(f"context: {pct}% ({win}/{src})")
                return
            scope = flags.get("scope", "chat")
            self._propose_command(
                "set_assistant_model",
                {"model": args[0], "scope": scope},
                notes=[f"scope: {scope}"] if scope != "chat" else None,
            )
            return
        if name == "exit":
            # v77-F3: explicit deck command to leave — bare exit/quit also work.
            print(_c(DIM, "exiting…"))
            self.quit = True
            return
        if name == "replay":
            # v77-F5: the resume summary withholds the scroll; this shows it.
            self.replay()
            return
        if name == "btw":
            if not args:
                print(usage)
                return
            # v67-F3 (R12b): a read-only side question — no cards, no
            # mutations, and it may run beside a pending confirmation.
            with self.client.stream(
                "POST",
                f"/api/chats/{self.chat_id}/messages",
                {"content": " ".join(args), "read_only": True},
            ) as response:
                render_turn(iter_sse(response), show_thinking=self.show_thinking, url=self.url)
            self._prompt_cache = None  # /btw is a model turn too
            return
        if name == "approve":
            if not args:
                print(usage)
                return
            # v51-F0: a pending card id resolves directly — the command IS the
            # operator's decision, no second prompt. Review ids fall through.
            card = self._pending_card(args[0])
            if card is not None:
                self._apply_card_verb(card, "confirm")
                return
            body = {"review_id": args[0]}
            if len(args) > 1:
                body["branch"] = args[1]
            self._propose_command("approve_review", body, auto_confirm=True)
            return
        if name == "deny":
            if not args:
                print(usage)
                return
            card = self._pending_card(args[0])
            if card is not None:
                self._apply_card_verb(card, "deny")
                return
            self._propose_command("deny_review", {"review_id": args[0]}, auto_confirm=True)
            return
        if name == "schedule":
            if len(args) < 4:
                print(usage)
                return
            self._propose_command(
                "propose_schedule",
                {
                    "name": args[0],
                    "repo": args[1],
                    "every": args[2],
                    "instructions": " ".join(args[3:]),
                },
            )
            return
        if name == "persona":
            if not args:
                print(usage)
                return
            self._propose_command("set_persona", {"text": " ".join(args)})
            return
        if name == "personality":
            if not args:
                print(usage)
                return
            self._propose_command("set_personality", {"value": " ".join(args)})
            return
        if name == "workon":
            if not args:
                print(usage)
                return
            body = {
                "path": args[0],
                "pack": flags.get("pack", "trusted_local_dev"),
                "phase": flags.get("phase", "build"),
            }
            # Preview first: the card must say exactly what confirming will do.
            preview = self.client.post("/api/workon/preview", body)
            notes = []
            if preview.get("would_git_init"):
                notes.append(
                    "not a git repo yet: confirming runs git init here — skep needs a "
                    "git baseline to make changes reviewable and revertible"
                )
            if preview.get("would_commit_baseline"):
                notes.append("the current tree will be committed as the baseline")
            notes.extend(str(warning) for warning in preview.get("warnings") or [])
            grants = (preview.get("project") or {}).get("dangerous_grant_warnings") or []
            if grants:
                notes.append(f"grants: {', '.join(grants)}")
            self._propose_command("workon", body, notes)
            return
        if name == "setup":
            if not args:
                print(usage)
                return
            repo = args[0]
            self.last_repo = repo
            project_id = _project_id_for_repo(repo)
            body = {
                "project_id": project_id,
                "name": project_id,
                "pack": flags.get("pack", "trusted_local_dev"),
                "phase": flags.get("phase", "build"),
                **_repo_binding(repo),
            }
            # Preview first, so the card states exactly what saving will grant.
            preview = self.client.post("/api/projects/preview", body)
            notes = []
            grants = preview.get("dangerous_grant_warnings") or []
            if grants:
                notes.append(f"grants: {', '.join(grants)}")
            seeded = preview.get("seeded_shell_commands") or []
            if seeded:
                notes.append(f"seeds {len(seeded)} toolchain command(s)")
            notes.append(_verify_pin_note(preview))
            self._propose_command("setup_project", body, notes)
            return

    def _propose_command(
        self,
        tool: str,
        args: dict[str, Any],
        notes: list[str] | None = None,
        *,
        auto_confirm: bool = False,
    ) -> None:
        proposed = self.client.post(
            f"/api/chats/{self.chat_id}/commands", {"tool": tool, "args": args}
        )
        for note in notes or []:
            print(_c(DIM, f"note: {note}"))
        card = {**proposed, "source": "operator"}
        if auto_confirm:
            # v63-F1: the operator typed the id — the command IS the decision
            # (v51-F0), so no second prompt; the card row still records it.
            # Without this, oneshot could never finish an /approve: each
            # attempt minted a new card its EOF-as-skip stdin cannot answer.
            _render_card(card)
            self._apply_card_verb(card, "confirm")
            return
        self._resolve_card(card)

    def replay(self) -> None:
        detail = self.client.get(f"/api/chats/{self.chat_id}")
        for message in detail["messages"][-REPLAY_MESSAGES:]:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role == "user" and content:
                print(_c(CYAN, PROMPT) + content)
            elif role == "assistant" and content:
                print(content)
            elif role == "tool":
                # v77-F4: replay renders the same thread shape as the live
                # stream — the stored row's result JSON, tolerant of non-JSON.
                name = str(message.get("tool_name") or "?")
                raw = str(message.get("content") or "")
                try:
                    result: Any = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    result = raw
                print("  " + _c(CYAN, f"▸ {name}"))
                print("    " + _c(DIM, _tool_summary(name, result, self.url)))

    def _status_prompt(self) -> str:
        """The status line prefix: `model: ... · ctx: NN% · you > ` with the
        percent colored by threshold (green <60, amber 60-79, red >=80).
        Off-TTY the bare glyph keeps every script pin byte-identical. Cached
        per model turn — deck reads trigger no fetch (v77-F2)."""
        if not _tty():
            return PROMPT
        if self._prompt_cache is None:
            self._prompt_cache = self._build_status_prompt()
        return self._prompt_cache

    def _build_status_prompt(self) -> str:
        try:
            detail = self.client.get(f"/api/chats/{self.chat_id}")
            ctx = detail.get("context", {})
            pct = int(ctx.get("percent", 0))
            if pct < 60:
                color = GREEN
            elif pct < 80:
                color = YELLOW
            else:
                color = RED
            # Effective model: chat override or the default from /api/llm/config
            model = (detail.get("chat") or {}).get("model")
            if not model:
                llm = self.client.get("/api/llm/config")
                model = llm.get("default_model", "unknown")
            return _pc(color, f"model: {model} · ctx: {pct}% · {PROMPT}")
        except Exception:
            # Refresh fails -> bare glyph; the prompt must never block.
            return _pc(CYAN, PROMPT)

    def loop(self) -> int:
        while True:
            try:
                line = input(self._status_prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            text = line.strip()
            if not text:
                continue
            if text in {"exit", "quit"}:
                return 0
            try:
                # A /command is intercepted BEFORE the model, like the web
                # composer (v25-F1) — parsed here, run against the HTTP API.
                if text.startswith("/"):
                    self.run_command(text)
                    if self.quit:
                        return 0
                else:
                    self.send(text)
            except ServeApiError as exc:
                print(_c(RED, f"error: {exc.detail}"), file=sys.stderr)


def _print_banner(client: ServeClient) -> None:
    """One dim line on entry: version, provider readiness, what needs a human.

    Walking back to the terminal after hours starts with "what needs me" —
    the doctor-advisory posture, one line, no ceremony.
    """
    from . import __version__

    status = client.get("/api/status")
    llm = client.get("/api/llm/config")
    ready = (
        f"assistant ready ({llm.get('default_model')})"
        if llm.get("configured") and llm.get("default_model")
        else "assistant unconfigured — finish Settings in the web UI"
    )
    waiting: list[str] = []
    approvals = int(status.get("pending_approvals") or 0)
    if approvals:
        waiting.append(f"{approvals} approval(s) waiting (/approvals)")
    # ponytail: newest 10 chats only — a startup banner, not an audit sweep.
    cards = 0
    for chat in client.get("/api/chats")["chats"][:10]:
        detail = client.get(f"/api/chats/{chat['chat_id']}")
        cards += sum(1 for a in detail["actions"] if a.get("status") == "proposed")
    if cards:
        waiting.append(f"{cards} card(s) pending")
    tail = " · ".join(waiting) if waiting else "nothing waiting on you"
    print(_c(DIM, f"skep {__version__} · {ready} · {tail}"))


def _run_oneshot(
    serve: ServeClient,
    message: str,
    *,
    show_thinking: bool,
    chat_id: str | None = None,
    continue_latest: bool = False,
    url: str = DEFAULT_URL,
    yes: bool = False,
) -> int:
    """The scripting face: one message, stream the reply, exit 0.

    v50-F1: cards resolve at the house prompt when stdin can answer — the
    black-box test's #1 finding was reading everything and acting on
    nothing. Without a TTY (cron, pipes), EOF reads as skip — never act —
    so scripts keep the old skip-and-report contract.

    v81-F12: ``--yes`` is the explicit opt-out — the operator pre-confirms
    THIS invocation's cards up front, so a mid-turn card no longer kills the
    answer off-TTY. The consent is per-invocation and operator-typed (I7);
    the model still never confirms anything (I6).

    v50-F2: --chat / --continue compose with --oneshot, so a multi-turn
    exchange keeps ONE transcript instead of re-asking from scratch. The
    flagless default stays a fresh chat per invocation — right for cron.
    """
    chat, _resumed = _resolve_chat(serve, chat_id=chat_id, continue_latest=continue_latest)
    repl = ChatRepl(serve, chat, show_thinking=show_thinking, url=url)
    repl.auto_confirm = yes
    # v48-F5: /commands are deck, never model — the same interception as the
    # REPL and the web composer (v25-F1).
    if message.startswith("/"):
        repl.run_command(message)
        return 0
    repl.send(message)
    return 0


def _resolve_chat(
    client: ServeClient, *, chat_id: str | None, continue_latest: bool
) -> tuple[dict[str, Any], bool]:
    """(chat record, is_resumed) for the requested session."""
    if chat_id:
        detail = client.get(f"/api/chats/{chat_id}")
        return dict(detail["chat"]), True
    if continue_latest:
        chats = client.get("/api/chats")["chats"]
        if chats:
            return dict(chats[0]), True
        print(_c(DIM, "no chats yet — starting a new one"))
    title = f"terminal {datetime.now():%Y-%m-%d %H:%M}"
    return dict(client.post("/api/chats", {"title": title, "source": "terminal"})), False


def run_chat(
    *,
    home: Path,
    url: str,
    chat_id: str | None = None,
    continue_latest: bool = False,
    show_thinking: bool = False,
    oneshot: str | None = None,
    yes: bool = False,
    client: HttpxClientLike | None = None,
) -> int:
    token_path = home / TOKEN_FILE
    if not token_path.is_file():
        print(DAEMON_HINT, file=sys.stderr)
        return 1
    serve = ServeClient(url, token_path.read_text(encoding="utf-8").strip(), client=client)
    try:
        if oneshot is not None:
            return _run_oneshot(
                serve,
                oneshot,
                show_thinking=show_thinking,
                chat_id=chat_id,
                continue_latest=continue_latest,
                url=url,
                yes=yes,
            )
        _print_banner(serve)
        chat, resumed = _resolve_chat(serve, chat_id=chat_id, continue_latest=continue_latest)
        repl = ChatRepl(serve, chat, show_thinking=show_thinking, url=url)
        if resumed:
            # v77-F5: one summary line instead of twenty messages of scroll —
            # what was withheld and the command that shows it are both named.
            detail = serve.get(f"/api/chats/{repl.chat_id}")
            messages = detail.get("messages") or []
            if messages:
                last = str(messages[-1].get("created_at") or "")
                held = f"{len(messages)} messages, last {last}".rstrip()
            else:
                held = "no messages yet"
            print(
                _c(
                    DIM,
                    f"resuming '{chat.get('title', '')}' ({repl.chat_id[:8]}) — {held} "
                    f"— /replay shows the last {REPLAY_MESSAGES}",
                )
            )
        return repl.loop()
    except httpx.ConnectError:
        print(DAEMON_HINT, file=sys.stderr)
        return 1
    except ServeApiError as exc:
        print(_c(RED, f"error: {exc.detail}"), file=sys.stderr)
        return 1


def cmd_chat(args: argparse.Namespace) -> int:
    # readline: importing it IS the feature — history + line editing on input().
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401
    _enable_completion()
    from .supervisor.cli_cmds import build_config

    config = build_config(args.home, None)
    return run_chat(
        home=config.home,
        url=args.url or os.environ.get("SKEP_SERVE_URL") or DEFAULT_URL,
        chat_id=args.chat_id,
        continue_latest=args.continue_latest,
        show_thinking=args.thinking,
        oneshot=args.oneshot,
        yes=bool(getattr(args, "yes", False)),
    )
