"""v104-F1: the operator gets the same verbs the Queen does (I5).

Three times the same defect shipped and three times it was found by hand:
v94-F5 (`coding_engine` — chat could set it, `project setup` could not),
v100-F9 (`verify_command`, same), v101-F13 (`repo_slug` — chat wrote two
kinds, REST three, the CLI one). Each was fixed one key at a time because
nothing measured the shape. The v103 field test found the fourth: to push a
branch and open a pull request the operator dropped to `uv run python -c
"from skep.supervisor.serve import actions; ..."` and `gh pr create`, because
neither verb has a face outside chat. Every hand-run git command is one the
audit trail never sees (I8) and the policy engine never gates (I5).

I5 says ONE authorization boundary. It does not say the operator gets the
narrow half of it, and a boundary wider for a small model than for the human
who owns the machine is not one boundary.

So: for every mutating chat verb, resolve the supervisor function its
`_execute_mutation` arm calls, and require that function to be reachable from
an operator surface too. Plain AST + text over the source files — no
framework, no new dependency, the `test_style_tokens.py` idiom.

The bookkeeping is the point (I10): `CHAT_ONLY` is the reviewed record of
every verb that owes no operator face — with a reason each, because a silent
skip list is how the first three gaps survived. `KNOWN_GAPS` is empty now
that v104 has closed its seven, and exists so the next round can record a
debt honestly rather than widening CHAT_ONLY to hide one.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "src/skep/supervisor"
TOOLS = SUPERVISOR / "serve/tools.py"

# Every surface a HUMAN reaches: the CLI groups (one `*_cmds.py` per family —
# globbed, so v105's new group is covered the day it lands) and the REST route
# modules the web UI drives. `serve/chat.py` and `serve/webhooks.py` are the
# model's own surface, not the operator's, and `serve/actions.py` is the shared
# verb layer being resolved TO — counting it would make every verb trivially
# "faced" and the gate would measure nothing.
OPERATOR_SURFACES: tuple[Path, ...] = (
    *sorted(SUPERVISOR.glob("*_cmds.py")),
    *sorted((SUPERVISOR / "serve").glob("*_cmds.py")),
    *(
        SUPERVISOR / "serve" / name
        for name in ("app.py", "notes.py", "memory.py", "registry.py", "run_status.py", "llm.py")
    ),
)

# The seven the v103 field test caught, and what v104-F2/F3/F4 close. They stay
# OUT of CHAT_ONLY on purpose: a branch or a pull request is exactly the kind of
# act the operator must be able to type. Every one already exists as a tested
# function in serve/actions.py (or serve/github.py) — nothing needs writing,
# they were simply never given a face. F2/F3/F4 delete their entries here; when
# this frozenset is empty the round is done and the constant goes with it.
GIT_FAMILY = frozenset(
    {
        "create_branch",
        "delete_branch",
        "merge_branch",
        "push_branch",
        "push_baseline",
        "merge_pr",
        "close_pr",
    }
)
KNOWN_GAPS: frozenset[str] = frozenset()
"""v104 closed all seven. Kept as a named concept, empty, because the NEXT
round that accepts a gap for one commit needs somewhere honest to record it —
the same reason UNIMPLEMENTED_CASTES survived v101-F2 (I8). An entry here is
a debt with a fix number beside it, never a place to park a verb."""

# The honest half. A verb here owes the operator nothing — either the human
# already has the capability more directly (a shell beats `run_shell`), or the
# thing it governs exists only to give the Queen tools, or the operator's face
# is the command deck (v25: `/…` is parsed client-side and lands as an
# `operator-command` action, so no Python surface mentions it). Reasons are
# load-bearing: an entry with an empty one fails the test, and a new entry is a
# judgement someone has to review rather than a line quietly added to a skip
# list.
CHAT_ONLY: dict[str, str] = {
    # --- Queen-side scoped tools: the operator IS the standing they borrow ---
    "read_file": "the operator has `cat`; the verb exists so the MODEL can read "
    "the host under the operator policy",
    "search_files": "same as read_file — the operator has ripgrep, the Queen "
    "needs a governed lane to it",
    "read_url": "the operator has a browser; the verb is the Queen's per-URL "
    "card, not a capability the human lacks",
    "run_shell": "lends the Queen the operator's own shell standing (v83-F9) — "
    "the operator already has the shell",
    "start_process": "same lane as run_shell, for daemons; the operator starts "
    "a dev server without asking skep",
    "stop_process": "the stop half of start_process — reaches only processes "
    "skep itself started from chat",
    "run_code": "a throwaway sandboxed script that can never land; `skep run` "
    "is the operator's dispatch face and a scratch script needs no supervisor",
    # --- learned grants: shaped by the run or the turn that was blocked ---
    "allow_shell_command": "the operator's face is `skep review <id> "
    "--allow-command`, which persists the same prefix and resumes; a standing "
    "CLI grant with no blocked step in front of it would be a second way to "
    "widen policy (I5)",
    "allow_env_bootstrap": "the packaged form of allow_shell_command — same "
    "allowlist, same operator face, plus `project setup`'s toolchain-seeded "
    "commands (v23-F4)",
    "apply_policy_preset": "a curated preset over that same allowlist; nothing "
    "reachable through it is unreachable via `skep review --allow-command` or "
    "`skep project setup`",
    "allow_fetch_domain": "writes a network rule governing the QUEEN's "
    "read_url; it grants the operator nothing they do not already have",
    "allow_mcp_tool": "an allow rule for a Queen-side MCP tool — no CLI run "
    "ever calls one, so there is nothing to authorize outside chat",
    "set_operator_policy": "the Queen's standing document (v52), governing "
    "Queen-side tools only; it has no meaning where there is no Queen turn",
    # --- project policy: `skep project setup` writes the same rows ---
    "attach_policy_group": "`skep project setup --group` writes the project's "
    "policy_groups list (v97-F4); the chat verb exists to add one without "
    "restating the setup",
    "detach_policy_group": "the same list, written by the same CLI flag — a "
    "setup without the group detaches it",
    "copy_project_policy": "chat-shaped convenience: `skep project setup` "
    "writes the destination's knobs directly, and copying reaches no key that "
    "setup cannot",
    # --- MCP + browser: the registry's only consumer is the Queen ---
    "register_mcp_server": "registered servers become QUEEN tools (chat.py "
    "assembles them); no CLI or worker path calls one, so administering them "
    "from chat is where they are used",
    "unregister_mcp_server": "the remove half of register_mcp_server, same reason",
    "call_mcp_tool": "an MCP tool call IS a Queen turn; the operator's "
    "equivalent is running the server's own command",
    "setup_browser": "registers the Playwright MCP server — same registry, and "
    "the operator's face is the command deck's `/browser`",
    # --- chat-shaped by construction ---
    "delegate_analysis": "spawns read-only LLM chats (ADR 0041); the artifact "
    "is a transcript, so there is nothing for a CLI to return",
    "forge_tool": "dispatches a coding run that authors a plugin — `skep run` "
    "submits the same work, and the patch still lands through the normal "
    "approval",
    "resume_run": "the operator's face is the command deck's `/resume` (v73: "
    "model-free), which is client-side and so invisible to this detector",
    "set_persona": "profile identity for chats; the operator's face is the "
    "deck's `/persona`",
    "set_personality": "THIS chat's reply style; the operator's face is the "
    "deck's `/personality`",
    "sync_notes": "mirrors notes into the operator's own Obsidian vault — the "
    "output is plain files the operator already owns",
    # --- ladder edges the CLI covers in one direction ---
    "suspend_skill_pack": "`skep skill promote` is the ladder's CLI face "
    "(v85-F5); the reverse edge stays chat-side and a suspended pack is "
    "re-promotable from either",
    "suspend_tool": "the same asymmetry for forged plugins — promote has an "
    "operator path, suspend is the chat undo beside it",
}


def _tools_tree() -> ast.Module:
    return ast.parse(TOOLS.read_text(encoding="utf-8"))


def _supervisor_names(tree: ast.Module) -> set[str]:
    """Names bound by a RELATIVE import, plus tools.py's own module-level defs.

    Relative import == skep's own code, which is the whole test: `subprocess`
    and `shlex` are absolute and stay out, because stdlib is not a supervisor
    verb and `subprocess.run(` appearing in some CLI module would forge a face.
    """
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
        for alias in node.names
    }
    return imported | {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _arm_verbs(test: ast.expr) -> list[str]:
    """The tool names one `if name == …` / `if name in (…)` arm answers to."""
    verbs: list[str] = []
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "name":
        operator, right = test.ops[0], test.comparators[0]
        if isinstance(operator, ast.Eq) and isinstance(right, ast.Constant):
            verbs.append(str(right.value))
        elif isinstance(operator, ast.In) and isinstance(right, ast.Tuple | ast.List | ast.Set):
            verbs += [str(e.value) for e in right.elts if isinstance(e, ast.Constant)]
    elif isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        for value in test.values:
            verbs += _arm_verbs(value)
    return verbs


def _mutation_calls() -> dict[str, set[str]]:
    """verb -> the supervisor functions its `_execute_mutation` arm calls.

    Matched from ANY module, never just `actions.` — a prototype that matched
    only `actions\\.` missed `merge_pr` and `close_pr`, which reach GitHub
    through `github.merge_pull_request` / `close_pull_request`. Both are
    genuinely unreachable by an operator, so the narrow matcher under-reported
    the very defect the gate exists to measure. `store.<method>` counts for the
    same reason: a verb that only writes a row still has a function to find.

    Shared plumbing is then dropped: a function several arms call
    (`repos_root`, `resolve_repo_arg`, `require_run`, `submit_run`) is not what
    the verb DOES, and leaving it in would let `merge_pr` claim a face because
    some CLI command also resolves a repo path. A verb whose calls are ALL
    shared keeps them — that is the honest answer for `quick_edit`, which is
    `submit_run` with a different prompt shape and rides `skep run`'s face.
    """
    tree = _tools_tree()
    internal = _supervisor_names(tree)
    execute = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_mutation"
    )
    arms: dict[str, list[ast.If]] = {}

    def collect(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.If):
                for verb in _arm_verbs(node.test):
                    arms.setdefault(verb, []).append(node)
                collect(node.orelse)  # the elif chain

    collect(execute.body)

    def called(nodes: list[ast.If]) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    # a module skep imported, or the store/config carried in
                    and (func.value.id in internal or func.value.id in {"store", "holder"})
                ):
                    names.add(func.attr)
                elif isinstance(func, ast.Name) and func.id in internal:
                    names.add(func.id)
        # lowercase only: `MCPServerConfig(...)` is a value the verb builds, not
        # a verb some CLI command could call instead.
        return {name for name in names if name[:1].islower()}

    raw = {verb: called(nodes) for verb, nodes in arms.items()}
    shared = Counter(name for names in raw.values() for name in names)
    return {verb: {n for n in names if shared[n] == 1} or names for verb, names in raw.items()}


def _operator_surface() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in OPERATOR_SURFACES)


def verbs_without_operator_face(calls: dict[str, set[str]], surface: str) -> set[str]:
    """The measurement. A verb has a face when ANY function it calls is CALLED
    from an operator surface — one of three CLI spellings landing on the same
    action is still one boundary.

    The lookbehind is not decoration: `registry._push_baseline` is a private
    clone-flow helper, and a bare substring match let `push_baseline` — one of
    the seven — claim a face it does not have.
    """
    return {
        verb
        for verb, functions in calls.items()
        if not any(
            re.search(rf"(?<![\w]){re.escape(function)}\s*\(", surface) for function in functions
        )
    }


def _missing() -> set[str]:
    return verbs_without_operator_face(_mutation_calls(), _operator_surface()) & MUTATING_TOOL_NAMES


def test_every_mutating_verb_has_an_operator_face_or_a_reason() -> None:
    """The gate. Unexplained asymmetry fails; so does bookkeeping that has gone
    stale, which is what makes KNOWN_GAPS an acceptance rather than a comment —
    the moment F2 gives `create_branch` a CLI face, its entry here must go."""
    missing = _missing()
    unexplained = missing - set(CHAT_ONLY) - KNOWN_GAPS
    assert not unexplained, (
        f"mutating chat verbs with no operator face and no ruling: {sorted(unexplained)} — "
        "give them a CLI/REST face, or a CHAT_ONLY entry saying why they owe none"
    )
    stale = (set(CHAT_ONLY) | KNOWN_GAPS) - missing
    assert not stale, f"these have an operator face now; drop them from the lists: {sorted(stale)}"


def test_every_chat_only_entry_carries_a_reason() -> None:
    """A skip list with no reasons is how v94-F5, v100-F9 and v101-F13 each
    went unnoticed until a human got stuck."""
    thin = sorted(verb for verb, reason in CHAT_ONLY.items() if len(reason.strip()) < 20)
    assert not thin, f"CHAT_ONLY entries with no real reason: {thin}"


def test_the_git_family_all_have_operator_faces() -> None:
    """v104's acceptance, as a standing gate. These seven were the round: every
    one existed as a tested function in serve/actions.py or github.py and was
    reachable only from chat, so the human typing commands had a strictly
    narrower authority surface than the model in the chat box (I5).

    They may never regress into CHAT_ONLY — an excuse is not a face, and moving
    one there would be closing the round by editing the test."""
    assert not (GIT_FAMILY & set(CHAT_ONLY)), "a gap explained away is not a gap closed"
    assert not (GIT_FAMILY & _missing()), (
        "the git family lost an operator face — see ADR 0050"
    )
    assert not (KNOWN_GAPS - GIT_FAMILY), "KNOWN_GAPS is a ledger, not a parking space"

def test_the_detector_never_goes_blind() -> None:
    """The failure mode of an AST gate is silence: a refactor of
    `_execute_mutation` (a dict dispatch, a decorator table) would leave every
    arm unresolved and the gate green on nothing. Every mutating verb must
    resolve to at least one function."""
    calls = _mutation_calls()
    unresolved = sorted(verb for verb in MUTATING_TOOL_NAMES if not calls.get(verb))
    assert not unresolved, f"no supervisor call resolved for: {unresolved}"


def test_a_verb_with_no_face_and_no_ruling_fails() -> None:
    """The negative. Injected data, not monkeypatched specs — the point is that
    the measurement itself catches a new verb, and a patched global that leaked
    would make every other test in the file lie."""
    surface = "def cmd_land(args):\n    return actions.land_run(store, task_id)\n"
    calls = {"land_run": {"land_run"}, "frobnicate": {"frobnicate_the_repo"}}
    assert verbs_without_operator_face(calls, surface) == {"frobnicate"}
    assert not {"frobnicate"} & (set(CHAT_ONLY) | KNOWN_GAPS)
