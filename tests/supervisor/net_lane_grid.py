"""ES-F21 (ADR 0059 P3-3): the probe grid for the Queen's network, MCP and browse lanes.

Eight home states x four chat roles x three learned-grant states x the
read_url, search_web, call_mcp_tool (mcp / email / browse servers) probes,
decided through what a chat turn calls: ``mutation_execution_decision`` for
read_url and call_mcp_tool, ``execute_mutation`` for read_url's execution
(the card lane after a confirm, and the redirect hops it re-decides), and
``execute_read_tool`` for search_web. No network: the fetch, the search and
every MCP server are fakes. ``tests/fixtures/policy_v3/pre_p3_3_net_verdicts.json``
is this grid answered BEFORE P3-3 moved the lanes onto ``policy_v3.decide()``;
``test_net_lanes_v3.py`` holds the new answers to it. Regenerate only with the
pre-P3-3 code: ``python -m tests.supervisor.net_lane_grid >
tests/fixtures/policy_v3/pre_p3_3_net_verdicts.json``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from unittest import mock

from skep.supervisor import RunStore
from skep.supervisor import policy_baseline as pb
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.mcp_client import MCPServerConfig, save_mcp_server
from skep.supervisor.policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
)
from skep.supervisor.serve import actions
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    execute_mutation,
    execute_read_tool,
    mutation_execution_decision,
)

from .conftest import no_template_role
from .queen_lane_grid import _baseline, build_home

# legacy: predates the baseline, nothing approved (+ a setup document and an
# operator overlay with network/mcp rules) · zero: a fresh store (O5) ·
# builder / assistant / operator: a locked baseline (+ the NET_EXTRAS) ·
# upgraded: a locked builder baseline on a home with no v2 scopes (B31) ·
# tampered: builder with its lock gone · stale-global: the `skep setup
# --template builder` document (+ the setup rules) under a locked assistant
# baseline (B30).
HOMES = (
    "legacy",
    "zero",
    "builder",
    "assistant",
    "operator",
    "upgraded",
    "tampered",
    "stale-global",
)
BASELINES = {
    "builder": "builder",
    "assistant": "assistant",
    "operator": "operator",
    "upgraded": "builder",
    "tampered": "builder",
    "stale-global": "assistant",
}
ROLES: tuple[str | None, ...] = (None, "builder", "assistant", "operator")
GRANTS = ("none", "session", "always")
GRANTED_URL = "https://granted.example/page"
GRANTED_TOOL = ("tracker", "update_issue")  # an allow_mcp_tool grant, un-annotated

FETCH_PROBES: tuple[tuple[str, str], ...] = (
    ("baseline-allow", "https://docs.python.org/3/"),
    ("template-github", "https://github.com/skep/skep"),
    ("baseline-deny", "https://evil.example/x"),
    ("unknown", "https://unknown.example/"),
    ("granted", GRANTED_URL),
    ("granted-path", "https://granted.example/other"),
    ("granted-query", "https://granted.example/page?token=abc"),
    ("userinfo", "https://user:pw@granted.example/page"),
    ("uppercase", "https://GRANTED.EXAMPLE/page"),
    ("port", "https://granted.example:8443/page"),
    ("ip", "http://93.184.216.34/"),
    ("loopback", "http://127.0.0.1:8765/api"),
    ("localhost", "http://localhost/"),
    ("setup-allow", "https://setup.example/"),
    ("setup-deny", "https://setupdeny.example/"),
    ("overlay-allow", "https://overlay.example/"),
    ("overlay-deny", "https://overlaydeny.example/"),
    ("malformed", "not a url"),
)
# The hosts a fetch's redirect chain is offered, in order (the granted lane
# re-decides every hop; the card lane follows them).
REDIRECT_HOPS = ("granted.example", "evil.example", "unknown.example", "docs.python.org")

RO = {"readOnlyHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}
# server -> (scope, ((tool, annotations | None), ...))
SERVERS: dict[str, tuple[str, tuple[tuple[str, dict[str, bool] | None], ...]]] = {
    "plain": (
        "mcp",
        (
            ("get_page", RO),
            ("delete_page", DESTRUCTIVE),
            ("list_things", None),
            ("budget_transfer", None),  # B09: "get" inside bud-get
            ("get_or_create_user", {"readOnlyHint": False}),  # a read verb that mutates
            ("search_index", RO),
            ("page_stats", RO),  # read-only annotated, no read verb
        ),
    ),
    "kb": ("mcp", (("get_doc", RO), ("write_doc", None))),
    "bank": ("mcp", (("get_balance", RO), ("transfer", DESTRUCTIVE))),
    "cal": ("mcp", (("get_events", RO), ("delete_event", DESTRUCTIVE))),
    "calendar": ("mcp", (("get_events", RO), ("delete_event", DESTRUCTIVE))),
    "tracker": ("mcp", (("update_issue", None),)),
    "mail": ("email", (("list_messages", RO), ("send_message", None))),
    "browser": (
        "browse",
        (
            ("browser_snapshot", RO),
            ("browser_click", None),
            ("browser_navigate", None),
            ("browser_type", None),
        ),
    ),
}
MCP_PROBES: tuple[tuple[str, str], ...] = (
    *((server, tool) for server, (_scope, tools) in SERVERS.items() for tool, _ann in tools),
    ("ghost", "get_thing"),  # a server that is not registered
)

# Rules every locked baseline in the grid carries beyond its template.
NET_EXTRAS = (
    {"scope": "network", "verdict": "allow", "action": "fetch", "pattern": "docs.python.org",
     "rule_id": "fetch-docs"},
    {"scope": "network", "verdict": "deny", "action": "fetch", "pattern": "evil.example",
     "rule_id": "no-evil"},
    {"scope": "mcp", "verdict": "deny", "action": "call", "pattern": "bank:*",
     "rule_id": "no-bank"},
    {"scope": "mcp", "verdict": "allow", "action": "call", "pattern": "kb:*", "rule_id": "mcp-kb"},
)  # fmt: skip


def _rule(rule_id: str, action: str, pattern: str) -> PolicyRule:
    return PolicyRule(rule_id=rule_id, action=action, pattern=pattern)


def build_net_home(root: Path, kind: str) -> dict[str, Path]:
    """A ``queen_lane_grid`` home of ``kind`` with the network/mcp rules above,
    the fake MCP servers registered, and (but on ``zero``) an operator overlay."""
    paths = build_home(root, "legacy" if kind == "operator" else kind)
    config = build_config(paths["personal"], None)
    template = BASELINES.get(kind)
    if template is not None:
        pb.write_lock(config.home, _baseline(template, *NET_EXTRAS), approved_by="op")
        if kind == "tampered":
            (config.home / "policy" / "baseline.lock").unlink()
    store = RunStore(config.db_path)
    try:
        for server, (scope, _tools) in SERVERS.items():
            save_mcp_server(
                store,
                MCPServerConfig(
                    server_id=server, transport="stdio", command=("fake",), scope=scope
                ),
            )
        if kind in ("legacy", "stale-global"):
            raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
            document = PolicyDocument.model_validate_json(raw) if raw else PolicyDocument()
            scopes = {scope.scope: scope for scope in document.scopes}
            network = scopes.setdefault("network", ScopePolicy(scope="network"))
            network.allow.append(_rule("setup-fetch", "fetch", "setup.example"))
            network.deny.append(_rule("setup-no-fetch", "fetch", "setupdeny.example"))
            mcp = scopes.setdefault("mcp", ScopePolicy(scope="mcp"))
            mcp.allow.append(_rule("setup-cal", "call", "cal:*"))
            document = document.model_copy(update={"scopes": list(scopes.values())})
            store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
        if kind != "zero":
            overlay = PolicyDocument(
                scopes=[
                    ScopePolicy(
                        scope="network",
                        allow=[_rule("overlay-fetch", "fetch", "overlay.example")],
                        deny=[_rule("overlay-no-fetch", "fetch", "overlaydeny.example")],
                    )
                ]
            )
            store.set_setting(OPERATOR_POLICY_SETTINGS_KEY, overlay.model_dump_json())
    finally:
        store.close()
    return paths


def fake_runner(config: MCPServerConfig, **_: Any) -> Any:
    """Every grid server answers tools/list with its annotations; a call echoes."""

    def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if method == "tools/list":
            return {
                "tools": [
                    {"name": tool, "description": tool, **({"annotations": ann} if ann else {})}
                    for tool, ann in SERVERS.get(config.server_id, ("mcp", ()))[1]
                ]
            }
        return {"content": [{"type": "text", "text": "ok"}]}

    return run


@contextlib.contextmanager
def fakes() -> Iterator[list[dict[str, Any]]]:
    """No byte leaves the machine: the fetch, the search and the MCP servers
    are fakes. Yields the list each fetch appends its call to."""
    fetched: list[dict[str, Any]] = []

    def fetch(url: str, *, redirect_guard: Any = None, max_bytes: int = 0, **_: Any) -> Any:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"read_url needs an http(s) URL, got {url!r}")
        hops = (
            "follow"
            if redirect_guard is None
            else "".join("T" if redirect_guard(hop) else "F" for hop in REDIRECT_HOPS)
        )
        fetched.append({"url": url, "hops": hops, "max_bytes": max_bytes})
        return {"url": url, "text": "page", "truncated": False}

    def search(query: str, **_: Any) -> list[dict[str, Any]]:
        return [{"title": "t", "href": "https://result.example/", "body": query}]

    with (
        mock.patch("skep.supervisor.serve.websearch.fetch_url_text", fetch),
        mock.patch("skep.supervisor.serve.websearch.search_web", search),
        mock.patch("skep.supervisor.mcp_client.runner_for_config", fake_runner),
    ):
        yield fetched


def show(decision: Any) -> str:
    """``verdict decided_by``; the ordinary card (None) is ``card -``."""
    if decision is None:
        return "card -"
    return f"{decision.verdict} {decision.decided_by or '-'}"


_DECIDED_BY = re.compile(r"decided_by: ([^)\s]+)\)")


def show_search(result: Mapping[str, Any]) -> str:
    """``allow <decided_by>`` when it searched, else ``deny <decided_by>``."""
    if result.get("results") and not result.get("error"):
        return f"allow {result.get('decided_by') or '-'}"
    found = (
        result.get("decided_by")
        or (_DECIDED_BY.search(str(result.get("error") or "")) or [None, "-"])[1]
    )
    return f"deny {found}"


def run_fetch(
    store: RunStore,
    holder: ConfigHolder,
    chat_id: str,
    url: str,
    decision: Any,
    fetched: list[dict[str, Any]],
) -> str:
    """read_url's execution: the lane's own decision when it runs in-turn,
    else None — the card lane after a human confirmed. ``ran <decided_by>
    <big|small> hops=<per REDIRECT_HOPS: T/F, or follow>`` or ``refuse``."""
    lane = decision if decision is not None and decision.allows_execution() else None
    before = len(fetched)
    try:
        result = execute_mutation(
            "read_url",
            {"url": url},
            store=store,
            holder=holder,
            runner=cast(Any, None),
            actor="op",
            decision=lane,
            chat_id=chat_id,
        )
    except Exception as exc:  # the grid records it
        return f"refuse {type(exc).__name__}"
    if len(fetched) == before:
        return f"refuse {result.get('error', '-')[:40]}"
    call = fetched[-1]
    size = "big" if call["max_bytes"] > 100_000 else "small"
    return f"ran {result.get('decided_by') or '-'} {size} hops={call['hops']}"


def _grant(store: RunStore, grant: str, chats: list[str]) -> None:
    # P3-4: a session keep holds in the chat it was made in — kept in each.
    for chat in chats if grant == "session" else chats[:1]:
        actions.remember_action_for_session(
            store, tool="read_url", args={"url": GRANTED_URL}, actor="op", tier=grant,
            chat_id=chat,
        )  # fmt: skip
    server, tool = GRANTED_TOOL
    actions.learn_policy_rule(
        store,
        rule_id=f"mcp:{server}:{tool}",
        action="call",
        pattern=f"{server}:{tool}",
        scope="mcp",
        provenance=("session:op" if grant == "session" else "allow-always:op"),
    )


def rows(root: Path, kind: str) -> Iterator[tuple[str, str]]:
    """Every grid cell of one home as ``(key, shown)``."""
    paths = build_net_home(root, kind)
    config = build_config(paths["personal"], None)
    store = RunStore(config.db_path)
    try:
        chats = {
            role: store.create_chat(  # "-": names no template (``no_template_role``)
                title=role or "none", model=None, role=role or no_template_role(config.home)
            ).chat_id
            for role in ROLES
        }
    finally:
        store.close()
    with fakes() as fetched:
        for grant in GRANTS:
            store = RunStore(config.db_path)
            try:
                if grant != "none":
                    _grant(store, grant, list(chats.values()))
            finally:
                store.close()
            for role in ROLES:
                chat = chats[role]
                store = RunStore(config.db_path)
                holder = ConfigHolder(config, store)
                try:
                    yield from _role_rows(store, holder, kind, role or "-", grant, chat, fetched)
                finally:
                    store.close()
            if grant != "none":  # the next grant state starts from no grant
                store = RunStore(config.db_path)
                try:
                    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
                    document = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                    document["learned"] = []
                    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
                finally:
                    store.close()


def _role_rows(
    store: RunStore,
    holder: ConfigHolder,
    kind: str,
    role: str,
    grant: str,
    chat: str,
    fetched: list[dict[str, Any]],
) -> Iterator[tuple[str, str]]:
    def key(lane: str, probe: str) -> str:
        return "|".join((kind, lane, probe, role, grant))

    for probe, url in FETCH_PROBES:
        try:
            decision = mutation_execution_decision(
                "read_url", {"url": url}, store=store, holder=holder, chat_id=chat
            )
            shown = show(decision)
        except Exception as exc:  # the grid records it
            decision, shown = None, f"error {type(exc).__name__}"
        yield key("fetch", probe), shown
        yield key("fetch-run", probe), run_fetch(store, holder, chat, url, decision, fetched)
    try:
        result = execute_read_tool(
            "search_web", {"query": "python docs"}, store=store, holder=holder, chat_id=chat
        )
        yield key("search", "query"), show_search(result)
    except Exception as exc:  # the grid records it
        yield key("search", "query"), f"error {type(exc).__name__}"
    for server, tool in MCP_PROBES:
        args = {"server_id": server, "tool": tool, "arguments": {}}
        try:
            shown = show(
                mutation_execution_decision(
                    "call_mcp_tool", args, store=store, holder=holder, chat_id=chat
                )
            )
        except Exception as exc:  # the grid records it
            shown = f"error {type(exc).__name__}"
        yield key("mcp", f"{server}:{tool}"), shown


def _freeze() -> None:  # pragma: no cover - run by hand against the pre-P3-3 code
    out: dict[str, str] = {}
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        for kind in HOMES:
            os.environ["HOME"] = str(Path(tmp) / kind / "fakehome")
            out.update(rows(Path(tmp) / kind, kind))
    os.chdir(cwd)
    lines = (f"{json.dumps(key)}: {json.dumps(out[key])}" for key in sorted(out))
    sys.stdout.write("{\n" + ",\n".join(lines) + "\n}\n")


if __name__ == "__main__":  # pragma: no cover
    _freeze()
