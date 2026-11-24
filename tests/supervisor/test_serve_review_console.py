"""Phase 10 R1 (10E): the review-first console's read-only routes — the
ledger verify badge, the searchable decision log, the one review queue, the
forged tools and the staged provider probe. Every one is a read (I5): the
verdicts stay on the routes that already existed."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, policy_v3
from skep.supervisor.policy_v3 import log_decision

from .conftest import serve_client


def _live(config: SupervisorConfig) -> Path:
    return config.audit_dir / "policy" / "decisions.jsonl"


# ---------- E1: the ledger verify badge ----------


def test_ledger_verify_route_reads_valid_then_broken_at_file_and_line(
    config: SupervisorConfig,
) -> None:
    client = serve_client(config)
    empty = client.get("/api/ledger/verify").json()
    assert empty["status"] == "VALID" and empty["chained"] == 0
    assert empty["line"] == "VALID: no chained rows"
    for n in range(3):
        log_decision(config.audit_dir, {"value": f"row {n}", "verdict": "allow"})
    valid = client.get("/api/ledger/verify").json()
    assert valid["status"] == "VALID" and valid["chained"] == 3 and valid["break"] is None
    assert valid["line"].startswith("VALID: 3 chained rows, head seq 3 sha256 ")
    live = _live(config)
    lines = live.read_text().splitlines()
    lines[1] = lines[1].replace("row 1", "row X", 1)
    live.write_text("".join(f"{line}\n" for line in lines))
    broken = client.get("/api/ledger/verify").json()
    assert broken["status"] == "BROKEN"
    assert broken["break"]["file"] == str(live) and broken["break"]["line"] == 2
    assert broken["line"].startswith(f"BROKEN at {live}:2: ")


def test_ledger_verify_route_says_degraded_coverage_when_a_checkpoint_was_never_kept(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 2)
    monkeypatch.setattr(policy_v3, "_checkpoint", lambda *a, **k: None)  # the write failed
    for n in range(4):
        log_decision(config.audit_dir, {"value": f"row {n}"})
    view = serve_client(config).get("/api/ledger/verify").json()
    assert view["status"] == "VALID" and view["checkpoints_missing"] == 2
    assert "coverage degraded" in view["line"]


# ---------- E1: the decision log, searched server-side ----------


def _seed_decisions(config: SupervisorConfig) -> None:
    rows = [
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "verdict": "allow"},
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "verdict": "deny"},
        {"task_id": "t2", "engine": "claude_code", "actor": "acp:claude", "verdict": "allow"},
        {"task_id": "t1", "engine": "claude_code", "actor": "cli", "verdict": "require_approval"},
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "source": "observed"},
    ]
    for n, row in enumerate(rows):
        log_decision(
            config.audit_dir,
            {"at": f"2026-11-2{n}T10:00:00Z", "scope": "shell", "action": "run", "value": f"c{n}"}
            | row,
        )


def test_decision_search_filters_task_engine_actor_and_verdict_on_the_server(
    config: SupervisorConfig,
) -> None:
    _seed_decisions(config)
    client = serve_client(config)

    def values(query: str) -> list[str]:
        found = client.get(f"/api/policy/decisions/search?{query}").json()
        return [row["value"] for row in found["rows"]]

    assert values("") == ["c4", "c3", "c2", "c1", "c0"]  # newest first
    assert values("task_id=t1&engine=codex") == ["c4", "c1", "c0"]
    assert values("actor=acp:*") == ["c4", "c2", "c1", "c0"]
    assert values("verdict=allow") == ["c2", "c0"]
    assert values("verdict=ask") == ["c3"]  # require_approval is an ask (I8)
    assert values("verdict=note") == ["c4"]  # a note is not a decision
    found = client.get("/api/policy/decisions/search?task_id=t1&verdict=deny").json()
    assert found["total"] == 1 and found["summary"]["deny"] == 1
    refused = client.get("/api/policy/decisions/search?verdict=maybe")
    assert refused.status_code == 400
    assert "allow, ask, deny, note" in refused.json()["detail"]


def test_decision_search_pages_with_a_cursor_that_new_rows_do_not_shift(
    config: SupervisorConfig,
) -> None:
    _seed_decisions(config)
    client = serve_client(config)
    first = client.get("/api/policy/decisions/search?limit=2").json()
    assert [r["value"] for r in first["rows"]] == ["c4", "c3"]
    assert first["total"] == 5 and first["next_cursor"] is not None
    log_decision(config.audit_dir, {"value": "c5", "verdict": "allow"})  # arrives between pages
    second = client.get(f"/api/policy/decisions/search?limit=2&cursor={first['next_cursor']}")
    page = second.json()
    assert [r["value"] for r in page["rows"]] == ["c2", "c1"]
    last = client.get(f"/api/policy/decisions/search?limit=2&cursor={page['next_cursor']}").json()
    assert [r["value"] for r in last["rows"]] == ["c0"] and last["next_cursor"] is None
    assert client.get("/api/policy/decisions/search?limit=0").status_code == 400


def test_decision_search_by_task_and_engine_answers_in_under_five_seconds(
    config: SupervisorConfig,
) -> None:
    """Phase 10 test 10.3: filter by task_id and engine in < 5 s."""
    path = _live(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:  # a big v1 log, written fast (the reader is what is timed)
        for n in range(40_000):
            row = {
                "at": "2026-11-20T10:00:00Z",
                "task_id": f"task-{n % 400}",
                "engine": ("codex", "claude_code")[n % 2],
                "actor": "acp:codex",
                "scope": "shell",
                "action": "run",
                "value": f"cmd {n}",
                "verdict": "allow",
            }
            handle.write(json.dumps(row) + "\n")
    client = serve_client(config)
    started = time.monotonic()
    found = client.get("/api/policy/decisions/search?task_id=task-8&engine=codex").json()
    assert time.monotonic() - started < 5.0
    assert found["total"] == 100
    assert all(r["task_id"] == "task-8" and r["engine"] == "codex" for r in found["rows"])
