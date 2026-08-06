"""v109-F3: landing surfaces share the task's words.

The Aug 3 field test showed one landing as three unrelated titles:
`land_run — blog/skep-is-live` (chat card), `approve_review — <uuid>` (gate
mirror), and `apply_patch: patch application review` (Approvals row). The
reason now names WHAT lands; the review card renders that reason instead of
its UUID; the verbs' results say what the model must not do next (I9).
"""

from __future__ import annotations

from skep.supervisor.serve.actions import landing_reason
from skep.supervisor.serve.cards import card_summary


def test_landing_reason_names_the_task_and_branch() -> None:
    assert landing_reason("Fix the bug", "skep/x") == 'land "Fix the bug" → skep/x'
    # No pinned branch → no guessed branch on the record (I8).
    assert landing_reason("Fix the bug", None) == 'land "Fix the bug"'
    assert landing_reason(None, None) == "land this run's patch"
    # Whitespace collapses; long briefs truncate at 80.
    assert landing_reason("a\n  b\tc", None) == 'land "a b c"'
    long = landing_reason("x" * 200, None)
    assert len(long) < 100 and long.endswith('…"')


def test_review_cards_headline_the_reason_not_the_uuid() -> None:
    """The gate mirror carries the approval's reason in args (run_status.py);
    the card's subject is that reason — a UUID identifies nothing to a human."""
    card = card_summary(
        "approve_review",
        {
            "review_id": "f7831247-2911-465b-9943-4e99933c1b85",
            "reason": 'land "blog: I\'m live" → blog/skep-is-live',
        },
        "Approving a pending review: applies the patch, or resumes a gated run.",
    )
    assert 'land "blog: I\'m live"' in card["headline"]
    assert "f7831247" not in card["headline"]

    # Without a reason (a bare model proposal), the review id still shows —
    # an empty subject would be worse than an ugly one.
    bare = card_summary("approve_review", {"review_id": "rev-1"}, "desc")
    assert "rev-1" in bare["headline"]


def test_shell_gate_cards_headline_the_command() -> None:
    """A worker's shell.run gate mirrors its reason too — the operator reads
    the command that is waiting, not which surface is asking."""
    card = card_summary(
        "approve_review",
        {"review_id": "rev-2", "reason": "shell.run requires approval for command: npm test"},
        "desc",
    )
    assert "npm test" in card["headline"]
