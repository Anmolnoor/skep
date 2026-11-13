"""ES-F9 (B43): the race helper, shown on a race the store already wins."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore

from .race import race


def test_racing_chat_inserts_each_get_their_own_row(tmp_path: Path) -> None:
    """Sixteen writers released at once on one chat: every insert returns its
    own row id, and a callable that raises comes back as its exception, in
    its slot, instead of killing the thread silently."""
    store = RunStore(tmp_path / "skep.db")
    chat_id = store.create_chat(title="t", model=None).chat_id

    def boom() -> int:
        raise RuntimeError("boom")

    writers = [
        lambda i=i: store.add_chat_message(chat_id, role="user", content=f"m{i}") for i in range(16)
    ]
    outcomes = race(*writers, boom)

    assert isinstance(outcomes[-1], RuntimeError)
    rows = {row.id: row.content for row in store.chat_messages(chat_id)}
    assert [rows[row_id] for row_id in outcomes[:-1]] == [f"m{i}" for i in range(16)]
