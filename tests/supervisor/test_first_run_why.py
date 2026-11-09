"""first-run O4 (ADR 0056 D6): a `why` under every verdict card while
``first_run.json`` is active; a per-turn tally; the offer; --quiet/--verbose."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from skep.cli import main
from skep.cli_chat import render_turn, run_chat
from skep.supervisor import SupervisorConfig
from skep.supervisor.onboard import (
    FIRST_RUN_FILE,
    note_turn,
    onboard_transparency,
    transparency_active,
    verdict_why,
)
from skep.supervisor.serve.chat import first_run_events

from .test_cli_chat import client, ollama, script_input  # noqa: F401  (fixtures)

ALLOW = {"verdict": "allow", "reason": "capability.allow.learned_rule", "decided_by": "learned/r1"}
DENY = {"verdict": "deny", "reason": "shell.deny.repo_target"}
TURN: list[tuple[str, dict[str, Any]]] = [
    ("tool", {"tool": "list_runs", "kind": "read", "result": {"runs": []}}),
    ("tool", {"tool": "run_shell", "decision": ALLOW, "result": {"ok": True, "result": {}}}),
    ("tool", {"tool": "run_shell", "decision": DENY, "result": {"ok": False, "error": "no"}}),
    ("action", {"action_id": "a1", "tool": "set_policy", "args": {"auto_approve": True}}),
    ("done", {"state": "awaiting_confirmation"}),
]


def test_verdict_why_names_rule_layer_and_reason() -> None:
    assert verdict_why(ALLOW) == (
        "allowed: rule r1 in the learned layer — capability.allow.learned_rule"
    )
    assert verdict_why(DENY) == "denied: shell.deny.repo_target"
    assert verdict_why({"verdict": "require_approval", "decided_by": "baseline/no_baseline"}) == (
        "asked: rule no_baseline in the baseline layer"
    )
    assert verdict_why(None) == "asked: no policy rule covers this tool, so it asks by default"


def test_every_verdict_gets_a_why_and_the_turn_a_tally(tmp_path: Path) -> None:
    onboard_transparency(tmp_path / "skep-home", verbose=True)
    events = list(first_run_events(iter(TURN), tmp_path))
    verdicts = [d for e, d in events if e == "action" or (e == "tool" and d.get("decision"))]
    assert len(verdicts) == 3 and all(d["why"] for d in verdicts)
    assert "why" not in events[0][1]  # a read is not a verdict
    assert [e for e, _ in events][-2:] == ["tally", "done"]
    tally = events[-2][1]
    assert tally["line"] == (
        "skep: 1 allowed · 1 asked · 1 denied — skep policy why for any of them"
    )
    record = json.loads((tmp_path / FIRST_RUN_FILE).read_text())
    assert (record["turns_seen"], record["verdicts_seen"]) == (1, 3)


def test_dismissed_or_never_onboarded_passes_through_untouched(tmp_path: Path) -> None:
    assert not transparency_active(tmp_path)  # no first_run.json: existing homes unchanged
    assert list(first_run_events(iter(TURN), tmp_path)) == TURN
    onboard_transparency(tmp_path / "skep-home", verbose=False)
    assert list(first_run_events(iter(TURN), tmp_path)) == TURN
    assert json.loads((tmp_path / FIRST_RUN_FILE).read_text())["turns_seen"] == 0


@pytest.mark.parametrize("bad", [None, "many", [1], {}])
def test_a_malformed_counter_reads_as_absent_never_a_crash(tmp_path: Path, bad: Any) -> None:
    """O4-F1: a non-int counter raised inside first_run_events and killed the
    chat stream mid-turn. A malformed record reads as absent — exactly like a
    malformed file — so the stream is byte-identical; note_turn never raises."""
    for key in ("turns_seen", "verdicts_seen"):
        record = {"started_at": "2026-11-09T00:00:00Z", "turns_seen": 0, "verdicts_seen": 0}
        (tmp_path / FIRST_RUN_FILE).write_text(json.dumps({**record, key: bad}))
        assert not transparency_active(tmp_path)
        assert list(first_run_events(iter(TURN), tmp_path)) == TURN
        assert note_turn(tmp_path, {"allowed": 1, "asked": 0, "denied": 0})["allowed"] == 1


def test_the_offer_comes_once_at_ten_turns_or_25_verdicts(tmp_path: Path) -> None:
    onboard_transparency(tmp_path / "skep-home", verbose=True)
    none = {"allowed": 0, "asked": 0, "denied": 0}
    offers = [bool(note_turn(tmp_path, none).get("offer")) for _ in range(12)]
    assert offers == [False] * 9 + [True, False, False]
    (tmp_path / FIRST_RUN_FILE).unlink()
    onboard_transparency(tmp_path / "skep-home", verbose=True)
    assert not note_turn(tmp_path, {**none, "allowed": 24}).get("offer")
    offer = note_turn(tmp_path, {**none, "denied": 1})["offer"]
    assert "skep onboard --quiet" in offer


# ---------- snapshots: the CLI output with the flag on and off ----------

SNAPSHOT_ON = """\
  ▸ list_runs
    ✓ 0 runs
    ran in-turn — read tool
  ▸ run_shell
    ✓ ok (2 chars)
    ran without asking — covered by capability.allow.learned_rule
    why: allowed: rule r1 in the learned layer — capability.allow.learned_rule
  ▸ run_shell
    ✗ no
    denied — shell.deny.repo_target
    why: denied: shell.deny.repo_target
skep: 1 allowed · 1 asked · 1 denied — skep policy why for any of them
"""

SNAPSHOT_OFF = """\
  ▸ list_runs
    ✓ 0 runs
    ran in-turn — read tool
  ▸ run_shell
    ✓ ok (2 chars)
    ran without asking — covered by capability.allow.learned_rule
  ▸ run_shell
    ✗ no
    denied — shell.deny.repo_target
"""


@pytest.mark.parametrize(
    ("verbose", "snapshot"), [(True, SNAPSHOT_ON), (False, SNAPSHOT_OFF)], ids=["on", "off"]
)
def test_cli_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verbose: bool, snapshot: str
) -> None:
    from skep.cli_chat import _render_card

    onboard_transparency(tmp_path / "skep-home", verbose=verbose)
    _state, actions = render_turn(cast(Any, first_run_events(iter(TURN), tmp_path)))
    assert capsys.readouterr().out == snapshot
    _render_card(actions[0])
    card = capsys.readouterr().out
    assert ("why: asked: no policy rule covers this tool" in card) is verbose


# ---------- the gate: a fresh chat shows why under every card; after dismiss, none ----------


def test_gate_fresh_chat_shows_why_then_none_after_dismiss(
    config: SupervisorConfig,
    client: TestClient,  # noqa: F811
    ollama: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--home", str(config.home.parent), "onboard", "--verbose"]) == 0
    capsys.readouterr()
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    script_input(monkeypatch, ["turn on auto approve"])  # EOF at the card = skip
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: set_policy" in out
    assert out.count("why: ") == out.count("confirm: ") == 1
    assert "skep: 0 allowed · 1 asked · 0 denied" in out

    # Dismiss on the REST face (the web's "Turn it down"), then a fresh chat.
    response = client.post("/api/onboard/transparency", json={"verbose": False})
    assert response.status_code == 200 and response.json()["dismissed"] is True
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    script_input(monkeypatch, ["turn on auto approve"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: set_policy" in out
    assert "why: " not in out and "skep: " not in out


def test_quiet_and_verbose_flip_the_flag(
    config: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    home = config.home.parent
    assert main(["--home", str(home), "onboard", "--quiet"]) == 0
    assert "why-lines: off" in capsys.readouterr().out
    assert not transparency_active(home)
    assert main(["--home", str(home), "onboard", "--verbose"]) == 0
    assert transparency_active(home)


def test_web_renders_why_and_tally() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "whyLine(d)" in source.split("const actionCard = (d)")[1].split("const receiptCard")[0]
    assert "whyLine(d)" in source.split("const receiptCard = (d)")[1].split("// v54-F2")[0]
    assert "tally: (d) =>" in source
    assert '"/api/onboard/transparency", { verbose: false }' in source
