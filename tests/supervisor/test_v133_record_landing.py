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

from skep.supervisor import RunStore, SupervisorConfig


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
