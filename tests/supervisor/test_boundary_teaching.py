"""v55-F3: the boundaries are taught where the Queen reads them.

The field test showed the Queen recommending allow_shell_command('git fetch')
— impossible by design, but its tool description named only push/commit, and
the system prompt had no registered-repo checklist. These pins keep the
load-bearing phrases from being silently dropped in a rewrite (the Queen is a
small model; descriptions ARE the guardrails it sees).
"""

from __future__ import annotations

from skep.supervisor.serve.chat import SYSTEM_PROMPT
from skep.supervisor.serve.tools import tool_description


def test_allow_shell_command_names_every_forbidden_git_verb() -> None:
    description = tool_description("allow_shell_command")
    for verb in ("push", "pull", "fetch", "add", "commit"):
        assert verb in description, f"allow_shell_command description must name git {verb}"
    assert "NEVER" in description
    assert "refresh_repo" in description


def test_register_repo_is_the_clone_path() -> None:
    description = tool_description("register_repo")
    assert "THE way" in description
    assert "never attempt a clone" in description


def test_repo_state_says_it_is_local_and_points_at_refresh() -> None:
    description = tool_description("repo_state")
    assert "LOCAL clone" in description
    assert "refresh_repo" in description


def test_system_prompt_carries_the_registered_repo_checklist() -> None:
    assert "checklist" in SYSTEM_PROMPT
    assert "refresh_repo" in SYSTEM_PROMPT
    assert "never be allowlisted" in SYSTEM_PROMPT


def test_system_prompt_carries_the_ask_list_rule() -> None:
    """v67-F2 (R11): a multi-ask prompt becomes a numbered list the Queen
    tracks to done/blocked — a dropped ask must be visible, never silent
    (field test: the Queen re-asked three times instead of tracking)."""
    assert "Asks: 1." in SYSTEM_PROMPT
    assert "numbers stable" in SYSTEM_PROMPT
    assert "done or name it blocked" in SYSTEM_PROMPT
    assert "never silently lose" in SYSTEM_PROMPT


def test_dispatch_framing_states_the_acceptance_check() -> None:
    """v67-F5/F6 (R10): every field-test verify failure was an improvised
    verify — the checklist, the dispatch_run description, and the seeded
    maintenance templates all demand the check up front."""
    assert "state the acceptance check" in SYSTEM_PROMPT
    assert "not ready to dispatch" in SYSTEM_PROMPT
    description = tool_description("dispatch_run")
    assert "acceptance check" in description
    assert "never a mega-task" in description

    from skep.supervisor.packs import builtin_policy_packs
    from skep.supervisor.projects import first_party_schedule_seeds

    pack = builtin_policy_packs()["trusted_local_dev"]
    assert "Verify by re-running" in pack.templates[0].instructions
    (seed,) = first_party_schedule_seeds(
        project_id="p", strategy="trusted_local_dev", phase="maintain"
    )
    assert "Verify by re-running" in seed.instructions


def test_high_traffic_descriptions_carry_when_not_and_the_trap() -> None:
    """v67-F5 (R4): the top carded classes teach when NOT to reach for them
    and the one known trap (the v64-F2 pattern, generalized)."""
    assert "NEVER chain read_url" in tool_description("read_url")
    assert "start_research" in tool_description("read_url")
    assert "Only a COMPLETED run with a patch can land" in tool_description("land_run")
    assert "NOT for remote URLs" in tool_description("workon")
    assert "ALREADY registered" in tool_description("setup_project")
    assert "RECURRING" in tool_description("propose_schedule")
    assert "not a schedule" in tool_description("propose_schedule")


def test_policy_preflight_is_taught_before_dispatch() -> None:
    """v55-F6: the Queen compares the task with effective_policy and says
    'not possible under the current policy' BEFORE dispatching, instead of
    letting the run hit a gate mid-flight."""
    assert "policy preflight" in SYSTEM_PROMPT
    assert "not possible under the current policy" in SYSTEM_PROMPT
    assert "never dispatch a run you expect to gate or fail" in SYSTEM_PROMPT

    description = tool_description("dispatch_run")
    assert "never dispatch into a known gate" in description


def test_state_reports_require_a_tool_result() -> None:
    """v58-F5: a Queen asked about a repo that does not exist must say so,
    never confabulate runs and ids (field case: a seven-run history invented
    for an unregistered 'skep-docs' repo, ids in a shape skep never mints)."""
    assert "without a tool result from THIS conversation" in SYSTEM_PROMPT
    assert "nothing was found" in SYSTEM_PROMPT
    assert "Never invent identifiers" in SYSTEM_PROMPT


def test_preflight_waits_for_the_verdict_and_big_asks_decompose() -> None:
    """v58-F2/F3: a proposed policy fix gates dispatch on its card verdict,
    and a big ask becomes small single-step dispatches, checked one by one."""
    assert "wait for that card's verdict" in SYSTEM_PROMPT
    assert "ONE step a worker can finish" in SYSTEM_PROMPT
    assert "never one mega-task" in SYSTEM_PROMPT
