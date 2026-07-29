"""v90-F2: the confirmation card summary — one line each, and no invented risk.

The card used to render a fixed sentence, the model-facing tool description
(long prose) and a raw arg dump: three restatements and no risk line. These pin
the three lines a human actually reads.
"""

from __future__ import annotations

from skep.supervisor.serve.cards import card_summary, headline, purpose, risk


def test_headline_is_the_thing_itself_not_a_sentence_about_it() -> None:
    summary = card_summary("run_shell", {"argv": ["uv", "run", "pytest", "-q"]})
    assert summary["headline"] == "run_shell — uv run pytest -q"
    # No prose wrapper, no "the assistant proposes".
    assert "proposes" not in summary["headline"]


def test_headline_falls_back_to_the_tool_name_for_an_unknown_shape() -> None:
    assert headline("some_new_verb", {"unexpected_key": "x"}) == "some_new_verb"
    assert headline("some_new_verb", {}) == "some_new_verb"


def test_headline_picks_the_subject_across_tool_families() -> None:
    assert headline("read_url", {"url": "https://example.com/x"}).endswith("https://example.com/x")
    assert headline("land_run", {"task_id": "019f-abc"}).endswith("019f-abc")
    assert headline("push_branch", {"branch": "skep/maintain"}).endswith("skep/maintain")
    # A string command is split and re-joined, so quoting is normalised.
    assert headline("run_shell", {"command": "echo 'a b'"}) == "run_shell — echo 'a b'"


def test_headline_is_bounded_and_single_line() -> None:
    line = headline("run_code", {"code": "x = 1\n" * 200})
    assert "\n" not in line
    assert len(line) <= 200


def test_purpose_is_the_first_sentence_only() -> None:
    description = "Dispatch one run. It blocks nothing. See also batch_dispatch for many."
    assert purpose(description) == "Dispatch one run."
    assert purpose("") is None
    # A description with no sentence break is returned whole, not truncated to "".
    assert purpose("Land the verified patch") == "Land the verified patch"


def test_purpose_strips_the_model_facing_propose_wrapper() -> None:
    """"PROPOSE …ing (requires user confirmation)" steers the Queen, not the
    human — the card's buttons already say nothing runs without a decision."""
    description = (
        "PROPOSE landing a completed run's patch (requires user confirmation). "
        "This is THE way to get finished work onto a branch."
    )
    assert purpose(description) == "Landing a completed run's patch."
    # The parenthetical may span dashes and semicolons before it closes.
    description = (
        "PROPOSE reading ONE public web page as text (requires user confirmation "
        "— the card shows the exact URL; nothing is fetched until the user "
        "confirms). EXCEPTION: granted domains read in-turn."
    )
    assert purpose(description) == "Reading ONE public web page as text."
    # Mid-sentence PROPOSE is content, not the wrapper — untouched.
    assert purpose("Never PROPOSE landing twice. More.") == "Never PROPOSE landing twice."


def test_purpose_does_not_end_the_sentence_at_an_abbreviation() -> None:
    description = "Adding ONE prefix to the allowlist, e.g. 'npm install'. Workers never need this."
    assert purpose(description) == "Adding ONE prefix to the allowlist, e.g. 'npm install'."


def test_benign_verbs_carry_no_risk_line() -> None:
    """An invented risk is as bad as a buried one."""
    assert risk("remember", {"text": "a note"}) is None
    assert risk("set_persona", {"name": "default"}) is None
    assert "risk" not in card_summary("remember", {"text": "a note"})


def test_risk_uses_the_engine_s_own_guard_classes() -> None:
    # Outbound content: never grantable, and the card says so (ADR 0044).
    outbound = risk("run_shell", {"argv": ["himalaya", "message", "send", "--to", "x@y"]})
    assert outbound is not None and "ADR 0044" in outbound

    # Privilege escalation names why it is special: it launders the guards below.
    escalation = risk("run_shell", {"argv": ["sudo", "systemctl", "restart", "x"]})
    assert escalation is not None and "launder" in escalation

    # Ops-mutating is approve-once, and says that rather than a generic warning.
    ops = risk("run_shell", {"argv": ["systemctl", "restart", "nginx"]})
    assert ops is not None and "approve-once" in ops


def test_risk_classes_cover_the_consequential_verbs() -> None:
    assert "remote" in (risk("push_branch", {"branch": "x"}) or "")
    assert "landing IS the commit" in (risk("land_run", {"task_id": "t"}) or "")
    assert "without asking" in (risk("set_policy", {}) or "")
    assert "cannot be restored" in (risk("delete_skill", {"name": "s"}) or "")


def test_network_risk_names_the_host() -> None:
    line = risk("read_url", {"url": "https://docs.example.com/page?q=1"})
    assert line is not None and "docs.example.com" in line


def test_card_summary_omits_absent_parts_entirely() -> None:
    """Absent, not empty-string — a blank line reads as a missing answer."""
    summary = card_summary("remember", {"text": "x"}, description="")
    assert set(summary) == {"headline"}


def test_card_summary_tolerates_non_dict_args() -> None:
    summary = card_summary("run_shell", ["not", "a", "dict"], description="Run it.")
    assert summary["headline"] == "run_shell"
