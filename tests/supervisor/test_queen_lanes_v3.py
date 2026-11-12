"""ADR 0059 P3-2: the Queen's file and shell lanes decide through
``policy_v3.decide()``. Nothing they decide is more permissive than before,
except a human-approved learned grant answering a catch-all ask."""

from __future__ import annotations

import io
import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import _VERDICT_RANK
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve.actions import policy_decide, remember_action_for_session
from skep.supervisor.serve.fileio import queen_filesystem_decision
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import mutation_execution_decision, queen_shell_decision

from .queen_lane_grid import (
    FILE_PROBES,
    GRANTED,
    HOMES,
    ROLES,
    SHELL_PROBES,
    build_home,
    rows,
)

FROZEN: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_p3_2_queen_verdicts.json"
    ).read_text()
)
_RANK = {**_VERDICT_RANK, "card": _VERDICT_RANK["require_approval"]}
# The catch-all asks of the shipped roles (decision #1: defaults, not ceilings).
CATCH_ALL_ASKS = ("/shell-learnable", "/shell-gated")


def flips(kind: str, root: Path) -> Counter[str]:
    """Every cell of one home against its frozen pre-P3-2 answer, classified
    as ``looser`` (only a grant answering a catch-all ask may), ``stricter``
    or ``label``; raises on any other loosening."""
    seen: Counter[str] = Counter()
    for key, shown in rows(root, kind):
        old_verdict, old_by = FROZEN[key].split(" ", 1)
        new_verdict, new_by = shown.split(" ", 1)
        assert "error" not in (old_verdict, new_verdict), (key, FROZEN[key], shown)
        old, new = _RANK[old_verdict], _RANK[new_verdict]
        if new < old:
            grant = key.split("|")[4]
            granted = grant != "none" and new_by.startswith(
                f"grant:{'session' if grant == 'session' else 'global'}/"
            )
            # P3-4 (B101): on a home with no baseline a human's grant applies.
            answered = granted and (old_by.endswith(CATCH_ALL_ASKS) or kind == "zero")
            assert answered, f"more permissive: {key}: {FROZEN[key]} -> {shown}"
            seen["looser"] += 1
        elif new > old:
            seen["stricter"] += 1
        elif old_by != new_by:
            seen["label"] += 1
        if old_by.startswith("floor/"):
            assert new_by == old_by, (key, FROZEN[key], shown)
    return seen


@pytest.mark.parametrize("kind", HOMES)
def test_no_queen_lane_verdict_is_more_permissive_than_before(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property: home x role x grant x the file and shell probes, held to
    what the lanes decided before P3-2 (frozen). The one loosening allowed is
    decision #1 (B56): in a role chat a human's exact-argv grant answers the
    role's catch-all ask. A floor denial stays the same floor denial."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    seen = flips(kind, tmp_path)
    # A tampered baseline answers every cell: no grant applies there (P3-4:
    # on a home with no baseline one does, B101).
    assert bool(seen["looser"]) == (kind != "tampered"), seen


# --- the regressions, one per ledger row ---------------------------------------------------


@pytest.fixture()
def grid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """``grid(kind)`` → ``(paths, fmt, store, holder)`` over a queen_lane_grid
    home, with a fake user HOME and the cwd outside every root."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    opened: list[RunStore] = []

    def make(kind: str) -> tuple[dict[str, Path], dict[str, str], RunStore, ConfigHolder]:
        paths = build_home(tmp_path / kind, kind)
        monkeypatch.chdir(paths["outside"])
        config = build_config(paths["personal"], None)
        store = RunStore(config.db_path)
        opened.append(store)
        return paths, {k: str(v) for k, v in paths.items()}, store, ConfigHolder(config, store)

    yield make
    for store in opened:
        store.close()


def test_b56_a_session_grant_in_a_builder_chat_runs_the_exact_command_with_no_second_card(
    grid: Callable[..., Any],
) -> None:
    """Decision #1 (B56): keep=session on a builder-role chat's card, and the
    same exact command runs in-turn — the role's catch-all ask no longer
    out-ranks the grant. A different argv still cards; a daemon still cards."""
    _paths, fmt, store, holder = grid("builder")
    chat = store.create_chat(title="b", model=None, role="builder").chat_id
    args = {"command": GRANTED, "cwd": fmt["outside"]}

    def decide(tool: str = "run_shell", **extra: str) -> Any:
        return mutation_execution_decision(
            tool, {**args, **extra}, store=store, holder=holder, chat_id=chat
        )

    first = decide()
    assert (first.verdict, first.decided_by) == (
        "require_approval",
        "template:builder/shell-learnable",
    )
    # What _record_verdict does on confirm with keep="session".
    remember_action_for_session(
        store, tool="run_shell", args=args, actor="op", tier="session", chat_id=chat
    )
    again = decide()
    assert (again.verdict, again.decided_by) == (
        "allow",
        f"grant:session/shell:run:{GRANTED}@chat:{chat}",
    )
    assert decide(command=GRANTED + " --upload").verdict == "require_approval"
    assert decide("start_process").verdict == "require_approval"
    assert decide(command="git push origin main").verdict == "deny"  # the floor, above grants


def test_b30_a_stale_setup_document_stops_granting_reads_under_a_locked_baseline(
    grid: Callable[..., Any],
) -> None:
    """Decision #2 (B30): the `skep setup --template builder` document (with
    the legacy fs-read * allow) under a locked assistant baseline — a read
    outside the roots cards; its deny still refuses; inside a root reads."""
    _paths, fmt, store, holder = grid("stale-global")

    def read(path: str) -> Any:
        return queen_filesystem_decision(store, holder, action="read", path=path)

    outside = read(f"{fmt['outside']}/notes.txt")
    assert (outside.verdict, outside.reason, outside.decided_by) == (
        "require_approval",
        "filesystem.require_approval.outside_operator_roots",
        "default/unmatched",
    )
    denied = read(f"{fmt['outside']}/private/x.txt")
    assert (denied.verdict, denied.decided_by) == ("deny", "global/no-private")
    inside = read(f"{fmt['repo']}/README.md")
    assert (inside.verdict, inside.decided_by) == ("allow", "global/operator-root")
    wget = queen_shell_decision(store, holder, command="wget https://e.x", cwd=fmt["outside"])
    assert wget is not None and (wget.verdict, wget.decided_by) == ("deny", "global/no-wget")


def test_b30_with_no_baseline_the_setup_document_grants_as_before(
    grid: Callable[..., Any],
) -> None:
    from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
    from skep.supervisor.policy_templates import load_policy_template

    _paths, fmt, store, holder = grid("legacy")  # nothing ever approved
    document = load_policy_template("builder").document().model_dump(mode="json")
    for scope in document["scopes"]:
        if scope["scope"] == "filesystem":
            scope["allow"].append({"rule_id": "fs-read", "action": "read", "pattern": "*"})
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
    decision = queen_filesystem_decision(
        store, holder, action="read", path=f"{fmt['outside']}/notes.txt"
    )
    assert (decision.verdict, decision.decided_by) == ("allow", "global/fs-read")


def test_b31_an_upgraded_home_without_v2_scopes_composes_its_baseline(
    grid: Callable[..., Any],
) -> None:
    """B31: the approved baseline's deny and named ask reach the Queen's
    shell lane on a home with no policy_engine_v2_scopes (before: carded)."""
    _paths, fmt, store, holder = grid("upgraded")

    def shell(command: str) -> Any:
        return queen_shell_decision(store, holder, command=command, cwd=fmt["outside"])

    curl = shell("curl -sI https://example.com")
    assert (curl.verdict, curl.decided_by) == ("deny", "template:builder/no-curl")
    make = shell("make deploy now")
    assert (make.verdict, make.decided_by) == (
        "require_approval",
        "template:builder/ask-make-deploy",
    )
    npm = shell("npm install x")  # B03: the template's worker allows still stay out
    assert (npm.verdict, npm.decided_by) == ("require_approval", "template:builder/shell-learnable")


def test_b01_upgraded_a_legacy_fs_read_baseline_cards_outside_the_roots(
    grid: Callable[..., Any],
) -> None:
    """ADR 0059 Migration (B01-upgraded): a builder baseline still carrying the
    pre-ES-F2 `filesystem read *` allow — template_layer drops exactly it."""
    from skep.supervisor.policy_baseline import verify
    from skep.supervisor.policy_layers import legacy_fs_read_dropped, template_layer

    paths, fmt, store, holder = grid("legacy-fsread")
    decision = queen_filesystem_decision(
        store, holder, action="read", path=f"{fmt['outside']}/notes.txt"
    )
    assert (decision.verdict, decision.decided_by) == ("require_approval", "default/unmatched")
    assert legacy_fs_read_dropped(verify(paths["skep"])) == "builder"
    layer = template_layer(paths["skep"])
    assert layer is not None
    assert [r.rule_id for r in layer.rules if r.scope == "filesystem" and r.action == "read"] == []


@pytest.mark.parametrize("pattern", ["{repo}/README.md", "*"])
def test_an_explicit_ask_inside_an_operator_root_still_cards(
    grid: Callable[..., Any], pattern: str
) -> None:
    """D1: the operator roots answer only where no explicit rule matched. A
    baseline ask on a path inside a root — named, or the catch-all
    `filesystem read *` — cards the read; the root never lifts it."""
    from skep.supervisor import policy_baseline as pb

    from .queen_lane_grid import _baseline

    paths, fmt, store, holder = grid("legacy")
    readme = f"{fmt['repo']}/README.md"
    assert queen_filesystem_decision(store, holder, action="read", path=readme).verdict == "allow"
    ask = {"scope": "filesystem", "verdict": "require_approval", "action": "read",
           "pattern": pattern.format(**fmt), "rule_id": "ask-read"}  # fmt: skip
    pb.write_lock(paths["skep"], _baseline("builder", ask), approved_by="op")
    decision = queen_filesystem_decision(store, holder, action="read", path=readme)
    assert (decision.verdict, decision.decided_by) == (
        "require_approval",
        "template:builder/ask-read",
    )


def _cli(home: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    capsys.readouterr()
    assert main(["--home", str(home), "policy", *argv]) == 0
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def _hook(home: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The Claude Code hook for a Bash call with no cwd: the operator lane."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    with monkeypatch.context() as patch:
        patch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        patch.setattr("sys.stdout", out)
        assert main(["--home", str(home), "policy", "hook"]) == 0
    result: dict[str, Any] = json.loads(out.getvalue())["hookSpecificOutput"]
    return result


@pytest.mark.parametrize("kind", HOMES)
def test_the_queen_rest_cli_and_hook_give_one_verdict_and_one_label(
    kind: str,
    grid: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V1 for the Queen's own lanes: read_file and run_shell answer every
    probe with the verdict and ``decided_by`` REST `policy/decide`, `skep
    policy decide` and (no role) the hook give for the same request and role
    with no repo — on every home. Outside policy, and so outside the
    comparison: a nonexistent path (a validity check) and a line that runs in
    or reaches a registered repo (``run_repo``, the Queen's repo pen)."""
    paths, fmt, store, holder = grid(kind)
    # P3-4: REST, the CLI and the hook have no chat — an unbound grant is
    # what every face sees alike.
    remember_action_for_session(
        store, tool="run_shell", args={"command": GRANTED}, actor="op", tier="always"
    )
    hook = {"allow": "allow", "require_approval": "ask", "deny": "deny"}
    for role in ROLES:
        flags = ["--role", role] if role else []
        for _probe, template in FILE_PROBES:
            path = template.format(**fmt)
            queen = queen_filesystem_decision(
                store,
                holder,
                action="read",
                path=path,
                role=None if role is None else SHIPPED_ROLES[role],
            )
            if queen.reason == "filesystem.deny.no_such_path":
                continue
            value = str(Path(path).expanduser().resolve())  # what the Queen decides
            rest = policy_decide(holder, store, scope="filesystem", action="read", value=value,
                                 role=role)  # fmt: skip
            want = (rest["verdict"], rest["decided_by"])
            assert (queen.verdict, queen.decided_by) == want, (kind, role, _probe)
            cli = _cli(paths["personal"], capsys, "decide", "filesystem", "read", value, *flags)
            assert (cli["verdict"], cli["decided_by"]) == want, (kind, role, _probe)
        for _probe, template, where in SHELL_PROBES:
            if where != "run" or _probe == "repo-reach":
                continue
            command = template.format(**fmt)
            shell = queen_shell_decision(
                store,
                holder,
                command=command,
                cwd=fmt["outside"],
                role=None if role is None else SHIPPED_ROLES[role],
            )
            assert shell is not None
            rest = policy_decide(holder, store, scope="shell", action="run", value=command,
                                 role=role)  # fmt: skip
            want = (rest["verdict"], rest["decided_by"])
            assert (shell.verdict, shell.decided_by) == want, (kind, role, _probe)
            cli = _cli(paths["personal"], capsys, "decide", "shell", "run", command, *flags)
            assert (cli["verdict"], cli["decided_by"]) == want, (kind, role, _probe)
            if role is None:
                answer = _hook(paths["personal"], command, monkeypatch)
                assert answer["permissionDecision"] == hook[shell.verdict], (kind, _probe)
                assert str(shell.decided_by) in answer["permissionDecisionReason"], (kind, _probe)


@pytest.mark.parametrize("kind", ["stale-global", "legacy-fsread", "builder"])
def test_doctor_and_onboard_check_say_which_allows_stopped_granting(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-visible trace for the two P3-2 verdicts that get stricter:
    a stale setup document's allows (B30) and a legacy fs-read * (B01)."""
    from skep.status import build_status
    from skep.supervisor.onboard import onboard_check

    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    paths = build_home(tmp_path, kind)
    advisories = build_status(paths["personal"], provider_timeout=0.01)["advisories"]
    report = onboard_check(paths["skep"])["report"]
    stale = [line for line in advisories if line.startswith("policy document (skep setup")]
    legacy = [line for line in advisories if "legacy `filesystem read *`" in line]
    if kind == "stale-global":
        assert len(stale) == 1 and "under the locked assistant baseline" in stale[0], advisories
        assert "--template builder" in stale[0]
    if kind == "legacy-fsread":
        assert legacy == [
            "baseline: its legacy `filesystem read *` allow no longer applies — Queen reads "
            "outside the operator roots card; `skep policy approve --reapprove --template "
            "builder` adopts the shipped file"
        ]
    if kind == "builder":  # a shipped baseline and no setup document: nothing to say
        assert stale == legacy == []
    for line in stale + legacy:
        assert line in report


def test_b79_a_shell_card_names_the_rule_that_asked(grid: Callable[..., Any]) -> None:
    """B79: a Queen shell ask is a decision naming its rule, so the card's
    why-line reads it — never "no policy rule covers this tool" where the
    baseline's shell-learnable asked; that line only when nothing matched."""
    from skep.supervisor.onboard import verdict_why

    _paths, fmt, store, holder = grid("builder")
    asked = queen_shell_decision(store, holder, command="uv sync", cwd=fmt["outside"])
    assert asked is not None
    assert verdict_why(asdict(asked)) == (
        "asked: rule shell-learnable in the template:builder layer"
        " — shell.require_approval.shell-learnable"
    )
    _paths, fmt, store, holder = grid("legacy")
    unmatched = queen_shell_decision(store, holder, command="uv sync", cwd=fmt["outside"])
    assert unmatched is not None
    assert verdict_why(asdict(unmatched)) == "asked: no policy rule covers this, so it asks"


def test_the_floor_answers_the_queen_when_the_policy_document_will_not_load(
    grid: Callable[..., Any],
) -> None:
    """I4: both lanes consult the floor before they load a document, so a
    corrupt one still refuses a credential read and git push; off the floor
    it raises — never an allow."""
    from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY

    _paths, fmt, store, holder = grid("builder")
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, "{not json")
    key = queen_filesystem_decision(store, holder, action="read", path=f"{fmt['home']}/.netrc")
    assert (key.verdict, key.decided_by) == ("deny", "floor/floor.secret-reads")
    push = queen_shell_decision(store, holder, command="git push", cwd=fmt["outside"])
    assert push is not None
    assert (push.verdict, push.decided_by) == ("deny", "floor/floor.worker-git-hard-denies")
    with pytest.raises(ValueError):
        queen_shell_decision(store, holder, command="ls", cwd=fmt["outside"])
    with pytest.raises(ValueError):
        queen_filesystem_decision(store, holder, action="read", path=f"{fmt['repo']}/README.md")
