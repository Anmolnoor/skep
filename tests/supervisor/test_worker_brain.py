"""v127 Builder A: the builtin worker's brain (F1-F3).

F1 — the worker knows its pinned check and the real approval rules before it
plans: 5/5 scheduled maintenance runs on 2026-11-13 stopped pending approval
on ``ls -A`` / ``find`` / ``python3 check.py`` because the prompt never named
the pin and told the worker to write and run check.py.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.worker_contract import Permissions
from skep.workers import coding_minimal as coding_worker
from skep.workers.llm_plan import (
    LlmEditPlan,
    LlmToolPlan,
    _messages,
    plan_from_payload,
    react_conversation,
)
from skep.workers.worker_runtime import EventStream

PIN = "uv run pytest -q"
ALLOWLIST = [["git", "status"], ["uv", "run", "ruff", "check"]]


def _system(**kwargs: Any) -> str:
    return str(
        _messages(workspace=kwargs.pop("workspace"), instructions="x", **kwargs)[0]["content"]
    )


def test_prompt_with_a_pin_names_it_and_drops_check_py(tmp_path: Path) -> None:
    system = _system(workspace=tmp_path, verify_command=PIN, shell_allowlist=ALLOWLIST)
    assert f"skep re-runs `{PIN}` on your patch (G10)" in system
    assert f"Verify with exactly `{PIN}`" in system
    assert "Do not edit the files it executes unless the task asks" in system
    assert "check.py" not in system


def test_prompt_without_a_pin_keeps_the_stdlib_advice(tmp_path: Path) -> None:
    system = _system(workspace=tmp_path)
    assert "skep re-runs" not in system
    assert "verify with the standard library" in system
    assert "overwrites any existing check.py" in system


def test_prompt_renders_the_resolved_shell_allowlist(tmp_path: Path) -> None:
    system = _system(workspace=tmp_path, verify_command=PIN, shell_allowlist=ALLOWLIST)
    assert "these shell commands run without approval: `git status`, `uv run ruff check`" in system
    assert "anything else stops the run for the operator" in system
    empty = _system(workspace=tmp_path)
    assert "no shell command runs without approval" in empty


def test_prompt_states_the_baseline_and_summary_language(tmp_path: Path) -> None:
    system = _system(workspace=tmp_path)
    assert "Your worktree starts at the baseline commit" in system
    assert "earlier runs' edits are not here unless landed" in system
    assert "Write the summary in the language of the task" in system


def test_read_tool_advice_rides_only_the_react_prompt(tmp_path: Path) -> None:
    plan_system = _system(workspace=tmp_path, verify_command=PIN, shell_allowlist=ALLOWLIST)
    react_system = react_conversation(
        workspace=tmp_path, instructions="x", verify_command=PIN, shell_allowlist=ALLOWLIST
    )[0]["content"]
    for tool in ("repo.list_files", "repo.search", "filesystem.read"):
        assert f"{tool}" in react_system
    assert "need no approval" in react_system
    assert "need no approval" not in plan_system
    # the shared rules (pin + allowlist) still ride the react prompt verbatim
    assert f"Verify with exactly `{PIN}`" in react_system
    # worded from the resolved tools: a manifest without the read tools names none
    bare = react_conversation(
        workspace=tmp_path, instructions="x", tool_manifest=[{"id": "shell.run"}]
    )[0]["content"]
    assert "need no approval" not in bare


def test_injected_verify_step_uses_the_pin_argv() -> None:
    write = {"tool": "filesystem.write", "args": {"path": "a.txt", "content": "x"}}
    pinned = plan_from_payload({"steps": [write]}, default_verify=("uv", "run", "pytest", "-q"))
    assert isinstance(pinned, LlmToolPlan)
    assert pinned.steps[-1].args == {"argv": ["uv", "run", "pytest", "-q"], "purpose": "verify"}
    unpinned = plan_from_payload({"steps": [write]})
    assert isinstance(unpinned, LlmToolPlan)
    assert unpinned.steps[-1].args["argv"] == ["ls", "-la"]


def test_legacy_edit_plan_fallbacks_use_the_pin_argv() -> None:
    files = [{"path": "a.md", "content": "x"}]
    pin = ("npm", "test")
    for payload in (
        {"type": "llm_edit_plan", "summary": "s", "files": files},
        {"type": "llm_edit_plan", "summary": "s", "files": files, "verify": {}},
    ):
        plan = plan_from_payload(payload, default_verify=pin)
        assert isinstance(plan, LlmEditPlan)
        assert plan.verification.argv == pin


def test_request_edit_plan_injects_the_pin_into_a_verify_less_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from skep.profile import ProviderProfile
    from skep.workers import llm_plan

    reply = {"steps": [{"tool": "filesystem.write", "args": {"path": "a", "content": "x"}}]}
    monkeypatch.setattr(llm_plan, "_provider_chunks", lambda *a, **k: [json.dumps(reply)])
    provider = llm_plan.WorkerProvider(
        profile=ProviderProfile(name="ollama", model="m", endpoint="http://127.0.0.1:9")
    )
    plan = llm_plan.request_edit_plan(
        provider,
        workspace=tmp_path,
        instructions="x",
        network_allowlist=["127.0.0.1"],
        verify_command=PIN,
    )
    assert isinstance(plan, LlmToolPlan)
    assert plan.steps[-1].args["argv"] == PIN.split()


@pytest.mark.parametrize("react", [False, True])
def test_both_callers_pass_the_pin_and_allowlist(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, react: bool
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
            shell_allowlist=ALLOWLIST,
        ),
        budget=DEFAULT_BUDGET,
        verify_command=PIN,
        planning_protocol="react" if react else "plan",
    )
    stream = EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def capture(*args: object, **kwargs: Any) -> object:
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(
        coding_worker, "react_conversation" if react else "request_edit_plan", capture
    )
    with pytest.raises(Stop):
        coding_worker._execute(task, repo, stream, tmp_path / "out.json")
    assert seen["verify_command"] == PIN
    assert [list(p) for p in seen["shell_allowlist"]] == ALLOWLIST


def test_seeded_maintenance_text_points_at_the_pin() -> None:
    from skep.supervisor.packs import get_policy_pack
    from skep.supervisor.projects import first_party_schedule_seeds

    wanted = "If it pins none, make no change and report what you found."
    (seed,) = first_party_schedule_seeds(
        project_id="p", strategy="trusted_local_dev", phase="maintain"
    )
    assert wanted in seed.instructions
    assert "normal verification commands" not in seed.instructions
    assert "verify with exactly that check" in seed.instructions
    (template,) = get_policy_pack("trusted_local_dev").templates
    assert wanted in template.instructions
    assert "normal verification commands" not in template.instructions


# ---------- F2: a missed edit teaches the replan ----------


def _registry(workspace: Path) -> Any:
    from skep.workers.capabilities import CapabilityRegistry

    return CapabilityRegistry(workspace, emit=lambda *_args, **_kw: None)


def test_a_missed_edit_shows_the_closest_window_redacted(repo: Path) -> None:
    from skep.workers.capabilities import CapabilityDenied

    lines = [f"line {n}" for n in range(200)]
    lines[120] = "title: Skep is live!"
    # joined at runtime: the compiler folds `"a" + "b"`, so a key-shaped
    # literal would still sit in the .pyc that CI's gitleaks scans
    fake_key = "".join(["sk", "-", "abcdefghijklmnopqrstuvwx"])
    lines[121] = f"API_TOKEN={fake_key}"
    (repo / "post.mdx").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(CapabilityDenied) as caught:
        _registry(repo).invoke(
            "filesystem.edit", {"path": "post.mdx", "old": "title: Skep is LIVE", "new": "t"}
        )
    assert str(caught.value) == "old text not found in post.mdx"  # the head alone
    hint = caught.value.hint or ""
    assert "title: Skep is live!" in hint
    assert "API_TOKEN=***" in hint
    assert fake_key not in hint
    assert "line 0\n" not in hint  # the window, not the whole file
    assert len(hint.encode()) < 2_400


def test_a_missed_edit_with_no_close_line_shows_the_file_head(repo: Path) -> None:
    from skep.workers.capabilities import CapabilityDenied

    (repo / "big.txt").write_text("alpha\n" + "z" * 5_000 + "\n", encoding="utf-8")
    with pytest.raises(CapabilityDenied) as caught:
        _registry(repo).invoke(
            "filesystem.edit", {"path": "big.txt", "old": "0123456789", "new": "t"}
        )
    hint = caught.value.hint or ""
    assert "file head" in hint and "alpha" in hint
    assert len(hint.encode()) < 2_400


@pytest.mark.parametrize("name", [".env", "config/.env.production"])
def test_a_missed_edit_on_a_secret_file_shows_nothing(repo: Path, name: str) -> None:
    """v127-F2 review (I4/I11): the hint must not read through the secret
    floor — a 0-match edit on a tracked .env once sent its head (a live
    Stripe key redact() passed) to the provider and the audit trail."""
    from skep.workers.capabilities import CapabilityDenied

    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fake_key = "".join(
        ["sk", "_live_", "51HxQabcdefghijklmnop"]
    )  # runtime: not folded into the .pyc
    path.write_text(f"STRIPE={fake_key}\n", encoding="utf-8")
    with pytest.raises(CapabilityDenied) as caught:
        _registry(repo).invoke("filesystem.edit", {"path": name, "old": "STRIPE=x", "new": "t"})
    assert str(caught.value) == f"old text not found in {name}"
    assert caught.value.hint is None


def test_the_replan_shows_the_window_once_and_reads_markers_from_the_head() -> None:
    """The window rides its own field: the stderr line keeps the file name,
    the window appears once, and repo text that happens to say "permission
    denied" is not mistaken for the sandbox wall."""
    rec = coding_worker._PlanRecoverable(
        command="old text not found in notes.md",
        exit_code=None,
        stderr_tail="old text not found in notes.md",
        completed_steps=0,
        hint="; copy old exactly from the file. file head:\nerror: permission denied (doc)",
    )
    plan = plan_from_payload({"summary": "s", "steps": [{"tool": "git.status", "args": {}}]})
    _old, message = coding_worker._recovery_repair_context(plan, rec)
    assert "stderr: old text not found in notes.md" in message
    assert message.count("error: permission denied (doc)") == 1
    assert "sandbox wall" not in message


def test_a_file_named_in_the_task_reaches_the_snapshot_even_when_large(repo: Path) -> None:
    from skep.workers.llm_plan import _repository_snapshot

    from .conftest import git

    (repo / "src").mkdir()
    (repo / "src" / "named.mdx").write_text("N" * 20_000, encoding="utf-8")
    (repo / "other.mdx").write_text("O" * 20_000, encoding="utf-8")
    git(repo, "add", "src/named.mdx", "other.mdx")
    snapshot = _repository_snapshot(repo, instructions="Fix the typo in src/named.mdx.")
    assert "--- src/named.mdx" in snapshot
    assert "N" * 8_000 in snapshot and "N" * 8_001 not in snapshot  # the head
    assert "--- other.mdx" not in snapshot
    named_at = snapshot.index("--- src/named.mdx")
    assert named_at < snapshot.index("--- existing.py")  # named files go first
    assert _repository_snapshot(repo).count("--- src/named.mdx") == 0


# ---------- F3: the summary counts what the patch changed ----------


def _run_tool_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: list[dict[str, Any]]
) -> Any:
    import sys

    from skep.supervisor.contracts_io import read_result

    verify = [sys.executable, "-c", "print('ok')"]
    task = mint_task(
        workspace=repo,
        instructions="Bump the lockfile.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
            shell_allowlist=[[sys.executable, "-c"]],
        ),
        budget=DEFAULT_BUDGET,
        verify_command=shlex.join(verify),
    )
    plan = plan_from_payload(
        {
            # two sentences: the digest leads (v114-F4), so `changed N` shows
            "summary": "Bump the lockfile. Then verify it.",
            "steps": [*steps, {"tool": "shell.run", "args": {"argv": verify, "purpose": "verify"}}],
        }
    )
    stream = EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", lambda *a, **k: plan)
    out_path = tmp_path / "out.json"
    coding_worker._execute(task, repo, stream, out_path)
    return read_result(out_path)


def _shell_write(name: str) -> dict[str, Any]:
    import sys

    code = f"open({name!r}, 'w').write('x')"
    return {"tool": "shell.run", "args": {"argv": [sys.executable, "-c", code]}}


def test_a_shell_run_edit_is_counted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.workers.capabilities import CapabilityRegistry

    diffs: list[int] = []
    real_diff = CapabilityRegistry._git_diff

    def counted(self: Any) -> Any:
        diffs.append(1)
        return real_diff(self)

    monkeypatch.setattr(CapabilityRegistry, "_git_diff", counted)
    result = _run_tool_plan(repo, tmp_path, monkeypatch, [_shell_write("uv.lock")])
    assert "changed 1 file(s)" in result.summary, result.summary
    assert result.changed_files == []  # reverify's field stays the tool-write list
    assert len(diffs) == 1  # counted from the patch already written, no second diff


def test_a_granted_commit_does_not_hide_files(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit inside the run moves HEAD; the count diffs against the
    startup baseline, so the committed file still counts. (A plan cannot
    request git.commit — model-internal — so an allowlisted step commits.)"""
    import sys

    commit = (
        "import subprocess as s; open('a.txt', 'w').write('a'); "
        "s.run(['git', 'add', 'a.txt'], check=True); "
        "s.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a'], "
        "check=True)"
    )
    steps = [
        {"tool": "shell.run", "args": {"argv": [sys.executable, "-c", commit]}},
        _shell_write("b.txt"),
    ]
    result = _run_tool_plan(repo, tmp_path, monkeypatch, steps)
    assert "changed 2 file(s)" in result.summary, result.summary
