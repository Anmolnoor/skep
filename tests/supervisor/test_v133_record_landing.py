"""v133 B3 (I1 I2 I6 I7 I8 I13): the record and the landing card.

F11 a card row says who resolved it, from which surface, and what it showed;
F12 a landing past a failed G10 is one HIGH card bound to that record;
F13 the run notice's mark comes from G10, and policy rows read as policy;
F14 open_pr writes a ledger row; F15 a branch PR card names every run it
publishes; F16 worker and supervisor results are stated apart; F17 the
forensics report labels each row's kind; F18 policy views say their layer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve import actions


def _store(config: SupervisorConfig) -> RunStore:
    config.home.mkdir(parents=True, exist_ok=True)
    return RunStore(config.db_path)


# ---------- F11: a card row is a ledger row (J3, J6) ----------


def test_confirm_and_deny_store_actor_surface_and_evidence(config: SupervisorConfig) -> None:
    store = _store(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        evidence = {"runs": [{"task_id": "t1", "warning": "re-verification RAN and FAILED"}]}
        confirmed = store.add_chat_action(
            chat_id, tool="land_run", args={"task_id": "t1"}, card={"evidence": evidence}
        )
        assert store.claim_chat_action(confirmed)
        store.resolve_chat_action(
            confirmed, status="confirmed", result={"ok": True}, claimed=True,
            actor="chat-user", surface="web",
        )  # fmt: skip
        denied = store.add_chat_action(chat_id, tool="set_personality", args={"value": "x"})
        store.resolve_chat_action(
            denied, status="denied", result={"ok": False}, actor="channel:telegram:42"
        )

        yes = store.get_chat_action(confirmed)
        no = store.get_chat_action(denied)
        assert yes is not None and no is not None
        assert (yes.resolved_by, yes.resolved_surface) == ("chat-user", "web")
        assert yes.evidence == evidence  # what the card carried, kept at the verdict
        # a messenger actor names its surface; the actor string is never rewritten
        assert (no.resolved_by, no.resolved_surface) == ("channel:telegram:42", "telegram")
        assert no.evidence is None
    finally:
        store.close()


def test_an_old_store_opens_and_reads_old_rows_as_unknown(config: SupervisorConfig) -> None:
    store = _store(config)
    chat_id = store.create_chat(title="t", model=None).chat_id
    action_id = store.add_chat_action(chat_id, tool="set_personality", args={"value": "x"})
    store.resolve_chat_action(action_id, status="confirmed", result={"ok": True})
    store.close()
    conn = sqlite3.connect(config.db_path)
    for column in ("resolved_by", "resolved_surface", "evidence_json"):
        conn.execute(f"ALTER TABLE chat_actions DROP COLUMN {column}")
    conn.commit()
    conn.close()

    store = RunStore(config.db_path)  # the migration adds them back, nothing else changes
    try:
        old = store.get_chat_action(action_id)
        assert old is not None and old.status == "confirmed"
        assert (old.resolved_by, old.resolved_surface, old.evidence) == (None, None, None)
    finally:
        store.close()


def test_a_born_resolved_row_says_policy_decided_it(config: SupervisorConfig) -> None:
    store = _store(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        action_id = store.record_resolved_chat_action(
            chat_id, tool="read_file", args={"path": "a"}, result={"ok": True},
            decided_by="global/operator-root",
        )  # fmt: skip
        row = store.get_chat_action(action_id)
        assert row is not None
        assert row.status == "confirmed"  # the status readers filter on is unchanged
        assert row.resolved_by == "policy" and row.resolved_surface is None
    finally:
        store.close()


# ---------- F12: landing past a failed G10 is one informed HIGH card (J1) ----------


def _run_with_g10(
    store: RunStore, repo: Path, tmp_path: Path, *, outcome: str, confirmed: bool,
    exit_codes: list[int], worker: str = "passed",
) -> str:  # fmt: skip
    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text(
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n",
        encoding="utf-8",
    )
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    store.record_reverification(
        task.task_id, outcome=outcome, worker_outcome=worker, confirmed=confirmed,
        commands=["cd web && node smoke.cjs"], exit_codes=exit_codes,
        detail="Error: Cannot find module 'slugify' [command from the project's pinned"
        " verify_command]",
    )  # fmt: skip
    return str(task.task_id)


def test_a_failed_g10_landing_card_is_high_with_its_label_and_evidence(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card

    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        args = {"task_id": task_id}
        high = high_tier("land_run", args, store=store)
        assert high is not None and "Land despite failed supervisor verification" in high
        review_id = store.enqueue_approval(task_id, action="apply_patch", reason="land")
        assert high_tier("approve_review", {"review_id": review_id}, store=store) is not None

        recorded = recorded_card("land_run", args, high, store) or {}
        assert recorded["tier"] == "high"
        assert recorded["action_label"] == "Land despite failed supervisor verification"
        [run] = recorded["evidence"]["runs"]
        assert run["g10"] == {
            "command": ["cd web && node smoke.cjs"],
            "source": "the project's pinned verify_command",
            "ran": True,
            "exit_codes": [1],
            "outcome": "failed",
        }
        shown = card_summary("land_run", args, store=store, recorded=recorded)
        assert shown["action_label"] == "Land despite failed supervisor verification"
        assert shown["tier"] == "high"

        tampered = _run_with_g10(
            store, repo, tmp_path, outcome="tampered", confirmed=False, exit_codes=[0]
        )
        assert "test tampering" in (high_tier("land_run", {"task_id": tampered}, store=store) or "")
    finally:
        store.close()


def test_a_failed_g10_landing_note_starts_with_the_store_line(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: None)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        run = actions.require_run(store, task_id)
        actions.land_run(store, run, "chat-user", note="Verified independently: smoke exits 0")
        [approval] = store.approvals_for(task_id)
        note = approval.resolution_note or ""
        assert note.startswith("G10 failed:"), note
        assert "cd web && node smoke.cjs" in note and "exit codes [1]" in note
        assert note.endswith("Verified independently: smoke exits 0")  # kept, after the line
    finally:
        store.close()


def test_a_passed_g10_landing_card_and_note_are_unchanged(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module
    from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: None)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0]
        )
        args = {"task_id": task_id}
        assert high_tier("land_run", args, store=store) is None
        recorded = recorded_card("land_run", args, None, store) or {}
        assert "action_label" not in recorded and "tier" not in recorded
        assert "action_label" not in card_summary("land_run", args, store=store, recorded=recorded)
        actions.land_run(store, actions.require_run(store, task_id), "chat-user", note="ok")
        [approval] = store.approvals_for(task_id)
        assert approval.resolution_note == "ok"
    finally:
        store.close()


def test_a_g10_record_that_changed_after_the_card_was_drawn_refuses_the_confirm(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from skep.supervisor import apply as apply_module
    from skep.supervisor.serve.cards import high_tier, recorded_card
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    landed: list[str] = []

    def land(*a: Any, **_: Any) -> None:
        landed.append(a[1])

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", land)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        args = {"task_id": task_id}
        chat_id = store.create_chat(title="t", model=None).chat_id
        card = recorded_card("land_run", args, high_tier("land_run", args, store=store), store)
        action_id = store.add_chat_action(chat_id, tool="land_run", args=args, card=card)
        # the record moves on between the draw and the click
        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["cd web && node smoke.cjs"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        assert store.claim_chat_action(action_id)  # the confirm path's claim
        with pytest.raises(HTTPException) as refused:
            execute_mutation(
                "land_run", args, store=store, holder=ConfigHolder(config, store),
                runner=cast(Any, None), actor="chat-user", chat_id=chat_id,
            )  # fmt: skip
        assert refused.value.status_code == 409
        assert "changed since this card was shown" in str(refused.value.detail)
        assert landed == []
        assert all(a.status != "approved" for a in store.approvals_for(task_id))
    finally:
        store.close()
