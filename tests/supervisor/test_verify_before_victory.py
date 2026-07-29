"""v87-F4 (I2): the Queen reads the artifact before declaring victory.

Field test 2026-07-23: the Queen announced '✅ Run completed successfully!
228 words' for a run whose deliverable was fabricated placeholder content —
run state was all she ever saw. Two mechanisms close the loop: get_run's
patch_digest puts the deliverable's content in the same tool result as the
state, and a success-shaped answer about a completed run whose deliverable
was never touched this turn draws exactly one verify nudge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import DEFAULT_BUDGET
from skep.supervisor.serve.actions import patch_digest
from skep.supervisor.serve.chat import VERIFY_NUDGE

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client

_PATCH = """diff --git a/youtube-summaries/summary.md b/youtube-summaries/summary.md
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/youtube-summaries/summary.md
@@ -0,0 +1,3 @@
+# Summary
+[00:00] placeholder intro
+[00:30] placeholder middle
diff --git a/fetch.py b/fetch.py
index 2222222..3333333 100644
--- a/fetch.py
+++ b/fetch.py
@@ -1,2 +1,2 @@
-old = 1
+new = 2
"""


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _completed_run_with_patch(config: SupervisorConfig, repo: Path) -> str:
    """A completed run whose patch exists on disk and never landed."""
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="summarize", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        audit_dir = config.home / "audit" / task.task_id
        audit_dir.mkdir(parents=True, exist_ok=True)
        patch_file = audit_dir / f"{task.task_id}.patch"
        patch_file.write_text(_PATCH)
        store.add_artifact(
            task.task_id,
            kind="patch",
            audit_path=patch_file,
            sha256=hashlib.sha256(_PATCH.encode()).hexdigest(),
        )
        store.transition(task.task_id, "completed", None)
    finally:
        store.close()
    return task.task_id


def test_patch_digest_shows_content_with_honest_caps(config: SupervisorConfig, repo: Path) -> None:
    task_id = _completed_run_with_patch(config, repo)
    store = RunStore(config.db_path)
    try:
        digest = patch_digest(store, task_id)
    finally:
        store.close()
    assert digest is not None
    files = {entry["path"]: entry for entry in digest["files"]}
    summary = files["youtube-summaries/summary.md"]
    assert summary["added"] == 3 and summary["removed"] == 0
    # The CONTENT is in the digest — the fabricated timestamps are visible.
    assert "[00:00] placeholder intro" in summary["head"]
    assert files["fetch.py"]["added"] == 1 and files["fetch.py"]["removed"] == 1
    assert "note" not in digest  # nothing dropped, nothing claimed dropped


def test_get_run_carries_the_digest_for_completed_runs(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_read_tool

    task_id = _completed_run_with_patch(config, repo)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        detail = execute_read_tool("get_run", {"task_id": task_id}, store=store, holder=holder)
    finally:
        store.close()
    digest = detail["run"]["patch_digest"]
    assert "[00:00] placeholder intro" in digest["files"][0]["head"]


def test_success_prose_without_artifact_contact_draws_one_nudge(
    config: SupervisorConfig, repo: Path, ollama: FakeOllama
) -> None:
    _completed_run_with_patch(config, repo)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 5})
    ollama.script_reply("✅ Run completed successfully! All done.")
    ollama.script_reply("Checked: the summary file contains placeholder content.")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "did it work?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    bodies = ollama.chat_bodies()
    assert len(bodies) == 3
    # The nudge rides as the trailing transient system instruction, once.
    assert bodies[2]["messages"][-1]["role"] == "system"
    assert bodies[2]["messages"][-1]["content"] == VERIFY_NUDGE
    # The premature claim stays in history (honest), followed by the check.
    deltas = "".join(d["content"] for name, d in events if name is None)
    assert "placeholder content" in deltas


def test_success_prose_after_reading_the_digest_passes_untouched(
    config: SupervisorConfig, repo: Path, ollama: FakeOllama
) -> None:
    task_id = _completed_run_with_patch(config, repo)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("get_run", {"task_id": task_id})
    ollama.script_reply("✅ Run completed successfully — the summary reads well.")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "did it work?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    assert len(ollama.chat_bodies()) == 2  # no nudge round
