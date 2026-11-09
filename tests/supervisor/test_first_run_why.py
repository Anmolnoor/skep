"""first-run O4 (ADR 0056 D6): a `why` under every verdict card while
``first_run.json`` is active; a per-turn tally; the offer; --quiet/--verbose."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from skep.supervisor.onboard import (
    FIRST_RUN_FILE,
    note_turn,
    onboard_transparency,
    transparency_active,
    verdict_why,
)
from skep.supervisor.serve.chat import first_run_events

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
