"""B1-3 (ADR 0054 D2/D4/D6, A9/A10): the governor answers the permission hop from
policy, forwards what it cannot decide, refuses on the hard ring, and records it all."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.acp.govern import Governor, open_governor
from skep.supervisor.acp.proxy import Direction, Proxy
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY

from ..fixtures.acp import ACP_DIR, envelope_frames
from .conftest import predating_home

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
GOLDEN_FIXTURES = sorted(
    {
        path.parent / entry["fixture"]
        for path in ACP_DIR.glob("*/expected_probes.json")
        for entry in json.loads(path.read_text(encoding="utf-8"))
    }
)


def _demo_home(tmp_path: Path, *, fresh: bool = False) -> tuple[Path, Path]:
    """The B3 demo policy, as far as a repo's run policy can say it: allow
    `git status`, `ls`, `pnpm test` and `fs: workspace`; everything else is
    unmatched (ask -> forward); the floor denies `git push`. The run
    policy compiles allows only, so explicit deny / require_approval live on
    the operator lanes (`network.fetch`), which decide even with a repo.
    A home that predates O5 unless ``fresh`` (then the zero state binds)."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    db_path = build_config(home, None).db_path
    store = RunStore(db_path if fresh else predating_home(db_path))
    try:
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["git", "status"], ["ls"], ["pnpm", "test"]])
        network = {
            "scope": "network",
            "require_approval": [
                {"rule_id": "net:ask.example", "action": "fetch", "pattern": "ask.example"}
            ],
            "deny": [{"rule_id": "net:evil.example", "action": "fetch", "pattern": "evil.example"}],
        }
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"scopes": [network]}))
        store.add_project_policy(
            project_id="demo", name="demo", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="demo", binding_kind="repo_path", binding_value=str(repo.resolve())
        )
    finally:
        store.close()
    return home, repo


def _ledger(home: Path) -> list[dict[str, Any]]:
    path = build_config(home, None).audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _jsonrpc(frames: list[tuple[Direction, bytes]]) -> list[tuple[Direction, bytes]]:
    """T3 recordings carry no ids and name the method on responses (README (a));
    pair each agent request with the client's next response for that method so
    the proxy can correlate them. Live taps are real JSON-RPC and pass unchanged."""
    out: list[tuple[Direction, bytes]] = []
    open_ids: dict[str, int] = {}
    for direction, raw in frames:
        msg = json.loads(raw)
        if "jsonrpc" not in msg and direction == "a2c" and msg.get("kind") == "request":
            msg["id"] = open_ids[msg["method"]] = 9000 + len(out)
        elif "jsonrpc" not in msg and direction == "c2a" and msg.get("method") in open_ids:
            msg["id"] = open_ids.pop(msg.pop("method"))
        else:
            out.append((direction, raw))
            continue
        out.append((direction, json.dumps(msg, separators=(",", ":")).encode() + b"\n"))
    return out


def _replay(fixture: Path, tmp_path: Path) -> dict[str, Any]:
    home, repo = _demo_home(tmp_path)
    gov = open_governor(home, [fixture.parent.name])
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(to_client.write, to_agent.write, gov.handlers(), on_frame=gov.on_frame)
    frames = _jsonrpc(envelope_frames(fixture, str(repo)))
    for direction, raw in frames:
        proxy.feed(direction, raw)
    own = [raw for d, raw in frames if d == "c2a"]
    written = []
    for line in to_agent.getvalue().splitlines(keepends=True):
        if own and line == own[0]:
            own.pop(0)
        else:
            written.append(json.loads(line))
    assert not own, "every client line still reaches the agent"
    # What skep answered never reached the client; every other agent line did, in order.
    answered = {line["id"] for line in written}
    assert to_client.getvalue() == b"".join(
        raw for d, raw in frames if d == "a2c" and json.loads(raw).get("id") not in answered
    )
    rows = [{k: v for k, v in row.items() if k != "at"} for row in _ledger(home)]
    golden = {"to_agent": written, "ledger": rows}
    scrubbed: dict[str, Any] = json.loads(json.dumps(golden).replace(str(repo), "$REPO"))
    return scrubbed


@pytest.mark.parametrize("fixture", GOLDEN_FIXTURES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_replay_matches_the_golden(fixture: Path, tmp_path: Path) -> None:
    got = _replay(fixture, tmp_path)
    want = json.loads(fixture.with_suffix(".golden.json").read_text(encoding="utf-8"))
    assert got == want


def _frame(direction: Direction, **body: Any) -> tuple[Direction, bytes]:
    return direction, json.dumps({"jsonrpc": "2.0", **body}).encode() + b"\n"


def _ask(rid: int, tool_call: dict[str, Any], kinds: tuple[str, ...]) -> tuple[Direction, bytes]:
    options = [{"optionId": f"o-{k}", "name": k, "kind": k} for k in kinds]
    params = {"sessionId": "s", "toolCall": {"toolCallId": f"t{rid}", **tool_call}}
    return _frame("a2c", id=rid, method="session/request_permission", params={
        **params, "options": options,
    })  # fmt: skip


def _drive(
    gov: Governor,
    repo: Path,
    frames: list[tuple[Direction, bytes]],
    *,
    version: int = 1,
    caps: dict[str, Any] | None = None,
    new: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(to_client.write, to_agent.write, gov.handlers(), on_frame=gov.on_frame)
    init = {"protocolVersion": version, "clientCapabilities": caps or {}}
    head = [
        _frame("c2a", id=1, method="initialize", params=init),
        _frame("a2c", id=1, result={"protocolVersion": version, "agentInfo": {"name": "fx"}}),
    ]
    if new:
        head.append(_frame("c2a", id=2, method="session/new", params={"cwd": str(repo)}))
    for direction, raw in head + frames:
        proxy.feed(direction, raw)
    lines = [json.loads(x) for x in to_agent.getvalue().splitlines()]
    return [x for x in lines if x.get("id") not in (1, 2)], [
        json.loads(x) for x in to_client.getvalue().splitlines()
    ]


SEND = ("allow_once", "allow_always", "reject_once")


def test_operator_lane_deny_and_ask_and_no_reject_option(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    gov = open_governor(home, ["fx"])
    evil = {"kind": "fetch", "rawInput": {"url": "https://evil.example/x"}}
    asked = {"kind": "fetch", "rawInput": {"url": "https://ask.example/x"}}
    picked = {"outcome": {"outcome": "selected", "optionId": "o-allow_once"}}
    to_agent, _ = _drive(
        gov,
        repo,
        [
            _ask(5, evil, SEND),
            _ask(6, evil, ("allow_once",)),
            _ask(7, asked, SEND),
            _frame("c2a", id=7, result=picked),
        ],
    )
    assert [line["result"]["outcome"] for line in to_agent[:2]] == [
        {"outcome": "selected", "optionId": "o-reject_once"},
        {"outcome": "cancelled"},
    ]
    rows = _ledger(home)
    assert [(r["source"], r.get("rule_id")) for r in rows] == [
        ("operator", "net:evil.example"),
        ("operator", "net:evil.example"),
        ("operator", "net:ask.example"),
        ("forwarded", None),
    ]
    assert rows[-1]["note"] == "net:ask.example" and rows[-1]["outcome"] == "allow_once"
    assert rows[-1]["actor"] == "human@client" and rows[0]["actor"] == "acp:fx"


def test_repo_and_actor_overrides_reach_the_decide_rows(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    ls = _ask(5, {"kind": "execute", "rawInput": {"command": "ls"}}, SEND)
    gov = open_governor(home, ["fx"], repo=str(other), actor="ci-bot")
    _drive(gov, repo, [ls])
    # a loaded/resumed session never sends session/new: --repo still binds it
    _drive(open_governor(home, ["fx"], repo=str(other)), repo, [ls], new=False)
    first, resumed = _ledger(home)
    assert first["repo"] == str(other) and first["actor"] == "ci-bot"
    assert resumed["repo"] == str(other) and resumed["actor"] == "acp:fx"
    assert gov.session.cwd == str(other)


def _ran(tid: str, command: str) -> tuple[Direction, bytes]:
    update = {"sessionUpdate": "tool_call", "toolCallId": tid, "kind": "execute",
              "status": "completed", "rawInput": {"command": command}}  # fmt: skip
    params = {"sessionId": "s", "update": update}
    return _frame("a2c", method="session/update", params=params)


def test_a_loaded_session_binds_its_cwd_and_its_replayed_history_is_not_observed(
    tmp_path: Path,
) -> None:
    # B3-F2/F3 (T3 field test): T3 respawns the agent per turn and continues with
    # session/load — no session/new, and the agent replays every earlier tool call
    # before answering the load. The replay bound nothing (repo "") and wrote the
    # old calls as observed rows again, once per turn.
    # B3-F9: under T3 the replay was recorded even so — the window must not
    # depend on where the load's answer falls; the client's next prompt ends it.
    home, repo = _demo_home(tmp_path)
    load = _frame("c2a", id=3, method="session/load", params={"sessionId": "s", "cwd": str(repo)})
    prompt = _frame("c2a", id=4, method="session/prompt", params={"sessionId": "s", "prompt": []})
    _drive(
        open_governor(home, ["fx"]),
        repo,
        [load, _ran("old", "ls"), _frame("a2c", id=3, result=None), _ran("late", "ls"),
         prompt, _ran("new", "ls")],
        new=False,
    )  # fmt: skip
    rows = _ledger(home)
    assert [(r["source"], r["tool_call_id"], r["repo"]) for r in rows] == [
        ("observed", "new", str(repo))
    ]
    # v2 continues with session/resume — the cwd binds the same way
    gov = open_governor(home, ["fx"])
    resume = _frame("c2a", id=3, method="session/resume", params={"sessionId": "s", "cwd": "/r2"})
    _drive(gov, repo, [resume], new=False)
    assert gov.session.cwd == "/r2" and gov.session.replaying


def test_the_forwarded_row_names_the_asking_item_and_a_client_error(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    two = {"kind": "edit", "locations": [{"path": str(repo / "a.txt")}, {"path": "/etc/x"}]}
    broke = {"code": -1, "message": "client broke"}
    _drive(
        open_governor(home, ["fx"]), repo, [_ask(5, two, SEND), _frame("c2a", id=5, error=broke)]
    )
    *decided, forwarded = _ledger(home)
    assert [r["verdict"] for r in decided] == ["allow", "require_approval"]
    assert forwarded["value"] == "/etc/x" and forwarded["note"] == "unmatched"
    assert forwarded["outcome"] == "error"


def test_options_without_a_string_id_are_never_selected(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    ls = {"kind": "execute", "rawInput": {"command": "ls"}}
    bad_ids = [
        {"optionId": None, "kind": "allow_once"},
        {"optionId": {"x": 1}, "kind": "allow_once"},
    ]
    bad_kind = [{"optionId": "a", "kind": ["allow_once"]}]
    cancelled = {"outcome": {"outcome": "cancelled"}}
    frames = []
    for rid, options in ((5, bad_ids), (6, bad_kind)):
        msg = json.loads(_ask(rid, ls, ())[1])
        msg["params"]["options"] = options
        frames += [_frame("a2c", **msg), _frame("c2a", id=rid, result=cancelled)]
    to_agent, _ = _drive(open_governor(home, ["fx"]), repo, frames)
    assert [line["result"] for line in to_agent] == [cancelled, cancelled]  # only the client's
    rows = [r for r in _ledger(home) if r["source"] == "forwarded"]
    assert rows[0]["note"].startswith("allow, but the agent offered no allow_once")
    assert rows[1]["note"].startswith("skep could not decide")


def test_the_hard_ring_decides_the_quoted_argv_and_is_v1_only(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    caps = {"terminal": True, "fs": {"writeTextFile": True}}

    def terminal(rid: int, command: str, args: list[str]) -> tuple[Direction, bytes]:
        params = {"sessionId": "s", "command": command, "args": args}
        return _frame("a2c", id=rid, method="terminal/create", params=params)

    frames = [terminal(5, "bash", ["-c", "git push"]), terminal(6, "ls", ["-la", "my dir"])]
    to_agent, to_client = _drive(open_governor(home, ["fx"]), repo, frames, caps=caps)
    assert [line["id"] for line in to_agent] == [5]
    assert "floor.worker-git-hard-denies" in to_agent[0]["error"]["message"]
    assert to_client[-1]["id"] == 6
    assert [r["value"] for r in _ledger(home)] == ["bash -c 'git push'", "ls -la 'my dir'"]
    # A1: on v2 there is no hard ring, whatever the flags say: pass-through, no rows.
    v2 = open_governor(home, ["fx"])
    to_agent, to_client = _drive(v2, repo, frames[:1], version=2, caps=caps)
    assert v2.session.client_terminal and to_agent == [] and to_client[-1]["id"] == 5
    assert len(_ledger(home)) == 2


def test_govern_returns_the_agents_code_when_it_exits_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = _demo_home(tmp_path)
    read_end, write_end = os.pipe()  # our stdin stays open until after main returns
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end, "rb"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    argv = ["--home", str(home), "acp", "govern", "--", sys.executable, FAKE, "--exit-only", "5"]
    try:
        assert main(argv) == 5
    finally:
        os.close(write_end)
    # The abort happened at interpreter shutdown, so a real process is the witness.
    child = subprocess.Popen([sys.executable, "-m", "skep", *argv], stdin=subprocess.PIPE)
    try:
        assert child.wait(timeout=60) == 5
    finally:
        assert child.stdin is not None
        child.stdin.close()


def test_an_unreadable_store_forwards_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    repo.mkdir()
    build_config(home, None).db_path.mkdir(parents=True)  # sqlite cannot open a directory
    gov = open_governor(home, ["fx"])
    assert gov.store is None and gov.store_error
    to_agent, to_client = _drive(gov, repo, [
        _ask(5, {"kind": "execute", "rawInput": {"command": "ls"}}, SEND),
        _frame("c2a", id=5, result={"outcome": {"outcome": "cancelled"}}),
    ])  # fmt: skip
    assert to_agent == [
        {"jsonrpc": "2.0", "id": 5, "result": {"outcome": {"outcome": "cancelled"}}}
    ]
    assert to_client[-1]["method"] == "session/request_permission"
    (row,) = _ledger(home)
    assert row["source"] == "forwarded" and row["outcome"] == "cancelled"
    assert row["note"].startswith("skep could not decide")
    assert "store unreadable: every decision forwards" in capsys.readouterr().err


def test_govern_needs_an_agent_and_strips_the_separator(tmp_path: Path) -> None:
    from skep.cli import _parser

    with pytest.raises(SystemExit) as stop:
        main(["--home", str(tmp_path), "acp", "govern"])
    assert stop.value.code == 2
    args = _parser().parse_args(["acp", "govern", "--repo", "r", "--", "agent", "--repo", "x"])
    from skep.supervisor.acp_cmds import _agent_argv

    assert _agent_argv(args) == ["agent", "--repo", "x"] and args.repo == "r"


def test_govern_relays_a_real_child_and_returns_its_code(tmp_path: Path) -> None:
    home, repo = _demo_home(tmp_path)
    fixture = ACP_DIR / "claude-agent-acp" / "live-v1.ndjson"
    frames = envelope_frames(fixture, str(repo))
    trace = tmp_path / "wire.ndjson"
    done = subprocess.run(
        [sys.executable, "-m", "skep", "--home", str(home), "acp", "govern", "--",
         sys.executable, FAKE, str(fixture), str(repo)],
        input=b"".join(raw for d, raw in frames if d == "c2a"),
        capture_output=True,
        timeout=60,
        env={**os.environ, "SKEP_ACP_TRACE": str(trace)},
    )  # fmt: skip
    assert done.returncode == 3, done.stderr
    assert done.stdout == b"".join(raw for d, raw in frames if d == "a2c")
    assert "skep acp: governing claude-agent-acp (ACP v1) — rings: advisory —" in (
        done.stderr.decode()
    )
    assert {row["source"] for row in _ledger(home)} == {"observed"}
    # B3-F8: the trace is the fixture envelope — per direction, the frames as sent
    traced = envelope_frames(trace)
    for direction in ("c2a", "a2c"):
        assert [r for d, r in traced if d == direction] == [r for d, r in frames if d == direction]


def test_doctor_names_the_preset_modes_and_the_silent_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--home", str(tmp_path), "acp", "doctor", "--", sys.executable, FAKE, "--doctor"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "preset: claude-agent-acp" in out
    assert "modes: default, auto (default)" in out
    assert "rings: advisory" in out
    assert "WARNING: default mode auto never asks" in out
    not_acp = ["--home", str(tmp_path), "acp", "doctor", "--", sys.executable, "-c", "print('hi')"]
    assert main(not_acp) == 1
    assert "not an ACP agent" in capsys.readouterr().err


def test_install_hint_prints_the_t3_rows_with_an_absolute_executable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["acp", "install-hint"]) == 0
    out = capsys.readouterr().out
    assert "@agentclientprotocol/claude-agent-acp" in out
    assert "@agentclientprotocol/codex-acp" in out
    # B3-F1: the field test pasted a `a | b | c` line into ONE T3 row — print rows as rows.
    rows = [line.strip() for line in out.splitlines()]
    want = ["acp", "govern", "--", "npx", "-y", "@agentclientprotocol/claude-agent-acp"]
    assert rows[rows.index("acp") : rows.index("acp") + 6] == want
    assert "|" not in out
    exe = out.split("executable: ", 1)[1].splitlines()[0]
    assert Path(exe).is_absolute(), exe


def test_a_tampered_baseline_forwards_the_permission_hop_and_refuses_the_hard_ring(
    tmp_path: Path,
) -> None:
    """first-run O2 (ADR 0056 D1): a baseline that no longer matches its lock
    makes every advisory verdict `ask` and every hard-ring verdict `deny`."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template

    home, repo = _demo_home(tmp_path)
    supervisor = build_config(home, None).home
    pb.write_lock(supervisor, load_policy_template("builder"), approved_by="op")
    # `git status` is allowed by the demo policy and by the builder baseline.
    status = {"kind": "execute", "rawInput": {"command": "git status"}}
    caps = {"terminal": True}
    params = {"sessionId": "s", "command": "git", "args": ["status"]}
    terminal = _frame("a2c", id=6, method="terminal/create", params=params)
    gov = open_governor(home, ["fx"])
    to_agent, _ = _drive(gov, repo, [_ask(5, status, SEND), terminal], caps=caps)
    assert to_agent[0]["result"]["outcome"]["optionId"] == "o-allow_once"
    assert to_agent[1:] == []  # the hard ring passed it through
    path = pb.baseline_path(supervisor)
    path.chmod(0o644)
    layer = json.loads(path.read_text())
    layer["settings"]["default_network"].append("evil.example")
    path.write_text(json.dumps(layer, indent=2))
    gov = open_governor(home, ["fx"])
    to_agent, to_client = _drive(gov, repo, [_ask(7, status, SEND), terminal], caps=caps)
    # Advisory: forwarded to the human behind the client, never allowed.
    assert to_client[-1]["id"] == 7 and to_client[-1]["method"] == "session/request_permission"
    # Hard: no human hop, so deny.
    (refused,) = to_agent
    assert refused["id"] == 6
    assert "deny by baseline/baseline_tampered" in refused["error"]["message"]
    rows = [r for r in _ledger(home) if r.get("rule_id") == "baseline_tampered"]
    assert [r["verdict"] for r in rows] == ["require_approval", "deny"]
