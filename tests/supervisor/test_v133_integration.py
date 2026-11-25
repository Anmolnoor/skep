"""v133 integration: fixes that cross the builders' packages (Sol round 1)."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import SupervisorConfig

from .conftest import serve_client
from .test_v133_record_landing import _run_with_g10, _store


def test_an_operator_land_card_is_bound_to_the_g10_record_it_was_drawn_on(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """S3 (J1, I7): a typed /land records the evidence it showed, like the
    Queen's card, so a re-verification that changes before the click is seen."""
    client = serve_client(config)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        chat_id = store.create_chat(title="t", model=None).chat_id
        base = f"/api/chats/{chat_id}/commands"
        made = client.post(base, json={"tool": "land_run", "args": {"task_id": task_id}})
        assert made.status_code == 201, made.text
        action = store.get_chat_action(made.json()["action_id"])
        assert action is not None
        card = action.card or {}
        assert task_id in card["g10_bound"] and card["evidence"]["runs"]
        assert card["action_label"].startswith("Land despite")

        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["cd web && node smoke.cjs"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        refused = client.post(f"{base}/{action.action_id}/confirm?surface=web")
        result = refused.json()  # a command reports its refusal in the result
        assert result["ok"] is False, result
        assert "changed since this card was shown" in result["error"]
        assert all(a.status != "approved" for a in store.approvals_for(task_id))
    finally:
        store.close()
