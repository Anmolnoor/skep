"""ES-F25 (ADR 0059 P3-4): the probe grid for grants — every remember path x
where the grant could leak.

Five home states x three chat roles x the grant kinds a home can hold (none;
the Queen's run_shell / start_process / compound / read_url keeps at session
and always; the worker session tier; a worker "Allow &
remember"; the learned rows an old home holds, written raw) x the probes that
must not widen: the granted subject, the subject plus an argument, a
backgrounded line, a compound line, the same host on another path or with a
query, another chat, a role chat, another repo. Decided through what a chat
turn calls (``mutation_execution_decision``) and what a run compiles
(``resolve_run_policy`` + ``policy_decide`` with a repo).
``tests/fixtures/policy_v3/pre_p3_4_grant_verdicts.json`` is this grid answered
BEFORE P3-4; ``test_grants_v3.py`` holds the new answers to it. Regenerate only
with the pre-P3-4 code: ``python -m tests.supervisor.grant_grid >
tests/fixtures/policy_v3/pre_p3_4_grant_verdicts.json``. D14 then set every
chat ``-`` cell to the assistant cell, ``role:assistant/`` read
``role:notemplate/``: a role naming no template acts as assistant.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_resolver import PolicyResolutionError, resolve_run_policy
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
from skep.supervisor.serve import actions
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import mutation_execution_decision

from .conftest import no_template_role
from .queen_lane_grid import build_home

HOMES = ("legacy", "zero", "builder", "assistant", "tampered")
ROLES: tuple[str | None, ...] = (None, "builder", "assistant")
GRANTED = "python3 build.py --fast"
DAEMON = "npm run dev"
COMPOUND = "ls -la && " + GRANTED
URL = "https://granted.example/docs/page"
WORKER = ["make", "check"]
GRANTS = (
    "none",
    "shell-session",
    "shell-always",
    "bg-always",
    "compound-always",
    "fetch-session",
    "fetch-always",
    "legacy-shell",
    "legacy-fetch",
    "worker-session",
    "worker-remember",
)
SHELL_PROBES: tuple[tuple[str, str, str], ...] = (
    ("granted", "run_shell", GRANTED),
    ("granted-extra", "run_shell", GRANTED + " --upload"),
    ("granted-amp", "run_shell", GRANTED + " &"),
    ("compound", "run_shell", COMPOUND),
    ("compound-other", "run_shell", "pwd && " + GRANTED),
    ("bg-daemon", "start_process", DAEMON),
    ("bg-granted", "start_process", GRANTED),
    ("run-daemon", "run_shell", DAEMON),
)
FETCH_PROBES: tuple[tuple[str, str], ...] = (
    ("granted", URL),
    ("same-dir", "https://granted.example/docs/other"),
    ("other-path", "https://granted.example/collect"),
    ("query", URL + "?data=secret"),
    ("dotdot", "https://granted.example/docs/../collect"),
    ("http", "http://granted.example/docs/page"),
    ("other-host", "https://other.example/docs/page"),
)


def _accepts(func: Any, name: str) -> bool:
    return name in inspect.signature(func).parameters


def _raw_learned(store: RunStore, rule: dict[str, str]) -> None:
    """A learned row exactly as a pre-P3-4 home stored it (no new fields)."""
    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    document.setdefault("learned", []).append(rule)
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))


def grant(store: RunStore, holder: ConfigHolder, kind: str, chat: str, repo: Path) -> None:
    """Write grant ``kind`` the way its remember path does (Queen keeps in ``chat``)."""
    tier = "session" if kind.endswith("-session") else "always"

    def keep(tool: str, args: dict[str, Any]) -> None:
        takes_chat = _accepts(actions.remember_action_for_session, "chat_id")
        extra = {"chat_id": chat} if takes_chat else {}
        with contextlib.suppress(Exception):  # a refused keep stores nothing
            actions.remember_action_for_session(
                store, tool=tool, args=args, actor="op", tier=tier, **extra
            )

    if kind in ("shell-session", "shell-always"):
        keep("run_shell", {"command": GRANTED})
    elif kind == "bg-always":
        keep("start_process", {"command": GRANTED})
    elif kind == "compound-always":
        keep("run_shell", {"command": COMPOUND})
    elif kind in ("fetch-session", "fetch-always"):
        keep("read_url", {"url": URL})
    elif kind == "legacy-shell":
        _raw_learned(store, {"rule_id": f"shell:run:{GRANTED}", "action": "run",
                             "pattern": GRANTED, "scope": "shell",
                             "provenance": "allow-always:op"})  # fmt: skip
    elif kind == "legacy-fetch":
        _raw_learned(store, {"rule_id": "network:fetch:granted.example", "action": "fetch",
                             "pattern": "granted.example", "scope": "network",
                             "provenance": "allow-always:op"})  # fmt: skip
    elif kind == "worker-session":
        extra = {"repo": repo} if _accepts(actions.remember_commands_for_session, "repo") else {}
        actions.remember_commands_for_session(store, [WORKER], **extra)
    elif kind == "worker-remember":
        with contextlib.suppress(Exception):  # the grid shows a refused remember
            actions.persist_remembered_command(store, holder, repo, list(WORKER))


def _clear(store: RunStore) -> None:
    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    document["learned"] = []
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
    store.set_setting("session_allowed_shell_commands", [])
    store.set_setting("allowed_shell_commands", [])
    project = store.get_project_policy("app")
    if project is not None and "allowed_shell_commands" in project.policy:
        policy = {k: v for k, v in project.policy.items() if k != "allowed_shell_commands"}
        store.add_project_policy(
            project_id="app", name="app", strategy=project.strategy, phase=project.phase,
            policy=policy,
        )  # fmt: skip


def _show(decision: Any) -> str:
    if decision is None:
        return "card -"
    return f"{decision.verdict} {decision.decided_by or '-'}"


def _worker(store: RunStore, holder: ConfigHolder, repo: Path) -> str:
    """``in``/``out``: is the worker grant in a sandboxed run's allowlist there,
    plus what ``policy_decide`` with that repo says for it."""
    try:
        resolved = resolve_run_policy(
            store=store, config=holder.current, repo=repo, caste="coding", network=None,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode="sandbox",
        )  # fmt: skip
        held = "in" if WORKER in resolved.permissions.shell_allowlist else "out"
    except PolicyResolutionError:
        held = "error"
    record = actions.policy_decide(
        holder, store, scope="shell", action="run", value=shlex.join(WORKER), repo=str(repo)
    )
    return f"{held} {record['verdict']} {record['decided_by']}"


def rows(root: Path, kind: str) -> Iterator[tuple[str, str]]:
    paths = build_home(root, kind)
    other = root / "other-repo"
    other.mkdir()
    config = build_config(paths["personal"], None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="other", name="other", strategy="trusted_local_dev", phase="build",
            policy={},
        )  # fmt: skip
        store.add_project_binding(
            project_id="other", binding_kind="repo_path", binding_value=str(other.resolve())
        )
        stored = {role: role or no_template_role(config.home) for role in ROLES}  # "-": no template
        chats = {
            (role, which): store.create_chat(
                title=f"{role}-{which}", model=None, role=stored[role]
            ).chat_id
            for role in ROLES
            for which in ("a", "b")
        }
    finally:
        store.close()
    os.chdir(paths["outside"])
    repo = paths["repo"].resolve()
    for kind_of_grant in GRANTS:
        store = RunStore(config.db_path)
        holder = ConfigHolder(config, store)
        try:
            _clear(store)
            if kind_of_grant != "none":
                grant(store, holder, kind_of_grant, chats[(None, "a")], repo)
            for role in ROLES:
                for which in ("a", "b"):
                    chat = chats[(role, which)]
                    head = (kind, role or "-", which, kind_of_grant)

                    def key(face: str, probe: str, head: tuple[str, ...] = head) -> str:
                        k, r, w, g = head
                        return "|".join((k, face, probe, r, w, g))

                    for probe, tool, command in SHELL_PROBES:
                        args = {"command": command, "cwd": str(paths["outside"])}
                        try:
                            shown = _show(mutation_execution_decision(
                                tool, args, store=store, holder=holder, chat_id=chat
                            ))  # fmt: skip
                        except Exception as exc:
                            shown = f"error {type(exc).__name__}"
                        yield key("shell", probe), shown
                    for probe, url in FETCH_PROBES:
                        try:
                            shown = _show(mutation_execution_decision(
                                "read_url", {"url": url}, store=store, holder=holder, chat_id=chat
                            ))  # fmt: skip
                        except Exception as exc:
                            shown = f"error {type(exc).__name__}"
                        yield key("fetch", probe), shown
            yield (
                "|".join((kind, "worker", "repo-a", "-", "-", kind_of_grant)),
                _worker(store, holder, repo),
            )
            yield (
                "|".join((kind, "worker", "repo-b", "-", "-", kind_of_grant)),
                _worker(store, holder, other.resolve()),
            )
        finally:
            store.close()


def _freeze() -> None:  # pragma: no cover - run by hand against the pre-P3-4 code
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
