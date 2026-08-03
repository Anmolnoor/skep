"""G10: the supervisor re-runs the worker's verification — claims are not trusted."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.reverify import ReverifyOutcome, reverify

from .conftest import git


def _no_leftovers(repo: Path, config: SupervisorConfig) -> None:
    worktrees = list(config.worktrees_root.iterdir()) if config.worktrees_root.is_dir() else []
    assert worktrees == [], f"leftover worktrees (re-verify did not clean up): {worktrees}"
    listed = git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, f"git still tracks extra worktrees:\n{listed}"


def test_completed_run_is_reverified_and_confirmed(repo: Path, config: SupervisorConfig) -> None:
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert reverify is not None, "a completed run must be re-verified (G10)"
    assert reverify.outcome == "passed"
    assert reverify.confirmed is True
    assert reverify.commands == ['grep -q "value = 1" existing.py']
    assert reverify.exit_codes == [0]
    _no_leftovers(repo, config)


def test_lying_worker_is_caught_by_reverification(repo: Path, config: SupervisorConfig) -> None:
    """The worker claims completed+passed, but its patch fails the recorded command."""
    outcome = run_task(repo, "Pretend to fix it. MODE:liar", config=config)
    # The worker still self-reports completed — we record its claim faithfully…
    assert outcome.record.state == "completed"
    assert outcome.record.verification_outcome == "passed"

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    # …but the supervisor's independent re-run disagrees, and that is recorded loudly.
    assert reverify is not None
    assert reverify.outcome == "failed"
    assert reverify.confirmed is False
    assert reverify.exit_codes and reverify.exit_codes[0] != 0
    _no_leftovers(repo, config)


def _patchless(
    repo: Path, config: SupervisorConfig, changed_files: tuple[str, ...] | None
) -> ReverifyOutcome:
    return reverify(
        repo=repo,
        ref=None,
        patch_path=None,
        commands=["true"],
        config=config,
        profile_path=None,
        env={},
        changed_files=changed_files,
    )


def test_patchless_run_claiming_no_changes_is_not_applicable(
    repo: Path, config: SupervisorConfig
) -> None:
    """v65-F1: script/researcher runs and no-change audits have no patch BY
    DESIGN — the majority case of G10 must not wear the lying-worker shape."""
    outcome = _patchless(repo, config, changed_files=())
    assert outcome.outcome == "not_applicable"
    assert "changed no files" in outcome.detail
    assert outcome.exit_codes == []


def test_patchless_run_claiming_changes_stays_loudly_unavailable(
    repo: Path, config: SupervisorConfig
) -> None:
    """v65-F1: claimed changes WITHOUT a patch artifact is the suspicious case
    — it gets a louder detail, never the benign one."""
    outcome = _patchless(repo, config, changed_files=("a.py", "b.py"))
    assert outcome.outcome == "unavailable"
    assert "claimed 2 changed file(s)" in outcome.detail
    assert "no patch artifact" in outcome.detail


def test_patchless_run_with_unknown_result_is_nothing_to_reverify(
    repo: Path, config: SupervisorConfig
) -> None:
    """v81-F5 (revises v65-F1): no result envelope AND no patch — the run can
    never land, so G10 has nothing to protect; NOT CONFIRMED here was alarm
    noise, not honesty. The suspicious case (claimed changes, no patch) stays
    loudly unavailable above."""
    outcome = _patchless(repo, config, changed_files=None)
    assert outcome.outcome == "not_applicable"
    assert "nothing to re-verify" in outcome.detail


def _bind_project(config: SupervisorConfig, repo: Path, policy: dict[str, object]) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="pinned",
            name="pinned verification",
            strategy="trusted_local_dev",
            phase="build",
            policy=policy,
        )
        store.add_project_binding(
            project_id="pinned", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def test_worker_chosen_verification_can_confirm_a_broken_patch(
    repo: Path, config: SupervisorConfig
) -> None:
    """v88-F4: the gap, pinned as a test — this is pre-v88 behaviour.

    MODE:vacuous lies about nothing. It writes the same broken patch MODE:liar
    writes, but nominates ``true`` as its verification command. Re-running "the
    worker's verification" then confirms a patch that does not work. Without a
    project-pinned command this is what G10 does, and under a
    require_reverified auto-approval rule it is what lands.
    """
    outcome = run_task(repo, "Fix the bug. MODE:vacuous", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert reverify is not None
    assert reverify.commands == ["true"]
    assert reverify.confirmed is True  # the hole: a broken patch, confirmed
    assert "the worker's own verify step" in reverify.detail
    _no_leftovers(repo, config)


def test_pinned_verify_command_catches_the_vacuous_worker(
    repo: Path, config: SupervisorConfig
) -> None:
    """v88-F4 (I2): the project says what verification MEANS, so the same run
    that confirmed above now fails re-verification."""
    _bind_project(config, repo, {"verify_command": 'grep -q "value = 1" existing.py'})

    outcome = run_task(repo, "Fix the bug. MODE:vacuous", config=config)
    assert outcome.record.state == "completed"
    assert outcome.record.verification_outcome == "passed"  # the worker's claim, recorded

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert reverify is not None
    assert reverify.commands == ['grep -q "value = 1" existing.py']  # not the worker's "true"
    assert reverify.outcome == "failed"
    assert reverify.confirmed is False
    # I8: the record says which command it actually re-ran.
    assert "the project's pinned verify_command" in reverify.detail
    _no_leftovers(repo, config)


def test_pinned_verify_command_still_confirms_honest_work(
    repo: Path, config: SupervisorConfig
) -> None:
    """v88-F4 must not turn every run red — a real fix still passes the pin."""
    _bind_project(config, repo, {"verify_command": 'grep -q "value = 1" existing.py'})

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert reverify is not None
    assert reverify.outcome == "passed"
    assert reverify.confirmed is True
    _no_leftovers(repo, config)


def _uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `uv` that logs each call: argv, offline-env markers, and whether
    the patch had already landed when it ran."""
    bin_dir = tmp_path / "uvbin"
    bin_dir.mkdir()
    log = tmp_path / "uv.log"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        'patched="no"\n'
        '[ -e patched_marker.txt ] && patched="yes"\n'
        'echo "$1 nosync=${UV_NO_SYNC:-unset} cache=${UV_CACHE_DIR:-unset}'
        ' patched=$patched" >> "$UV_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, log


def _patch_adding_marker(tmp_path: Path) -> Path:
    patch = tmp_path / "marker.patch"
    patch.write_text(
        "diff --git a/patched_marker.txt b/patched_marker.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/patched_marker.txt\n"
        "@@ -0,0 +1 @@\n"
        "+patched\n",
        encoding="utf-8",
    )
    return patch


def test_uv_pin_primes_baseline_deps_then_verifies_offline(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v94-F7: the default pin (`uv run pytest`, v91-F1) exited 2 under the
    deny-all reverify profile — uv could not init its cache and every good uv
    patch read NOT CONFIRMED (field run 019f9ea0). Reverify now primes the
    BASELINE env (before the patch applies, so patch code never gets the
    network) and runs the pin offline against a workspace-local cache."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pyproject")
    bin_dir, log = _uv_stub(tmp_path)

    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )

    assert outcome.outcome == "passed", outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    # The prime ran first, online (no UV_NO_SYNC), against the pristine
    # baseline — the patch had not been applied yet.
    assert lines[0].startswith("sync nosync=unset")
    assert "patched=no" in lines[0]
    assert "cache=" in lines[0] and "cache=unset" not in lines[0]
    # The pinned command ran second, offline, after the patch landed.
    assert lines[1].startswith("run nosync=1")
    assert "patched=yes" in lines[1]
    assert "primed from the baseline" in outcome.detail


def test_non_uv_pin_never_primes(repo: Path, config: SupervisorConfig, tmp_path: Path) -> None:
    """The priming lane is uv-shaped only; every other pin behaves as before."""
    bin_dir, log = _uv_stub(tmp_path)
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["true"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )
    assert outcome.outcome == "passed"
    assert not log.exists()


def test_failed_prime_is_unavailable_not_patch_guilt(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """A prime that cannot complete is a supervisor-side environment problem —
    the honest shape is 'unavailable' (like exit 127), never a failed patch."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pyproject")
    bin_dir, _log = _uv_stub(tmp_path)
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")

    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert outcome.outcome == "unavailable"
    assert "prime" in outcome.detail
    assert "was not run" in outcome.detail


def test_slug_bound_pin_survives_the_run_task_fallback(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """The authwapi acceptance hole (019fc724): a managed clone's project is
    bound by repo_slug, run_task's safety-net pin lookup offered only the
    repo_path candidate, and G10 silently degraded to the worker's own verify
    step — confirmed=true on `git diff --check` while `npm test` sat pinned.
    The fallback must offer the slug for repos under <home>/repos."""
    slug = "slug-fixture"
    repo = config.home.parent / "repos" / slug
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "existing.py").write_text("value = 0\n")
    git(repo, "add", "existing.py")
    git(repo, "commit", "-qm", "seed")

    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="slugged",
            name="slug bound",
            strategy="trusted_local_dev",
            phase="build",
            policy={"verify_command": 'grep -q "value = 1" existing.py'},
        )
        store.add_project_binding(
            project_id="slugged", binding_kind="repo_slug", binding_value=slug
        )
    finally:
        store.close()

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        reverify_record = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert reverify_record is not None
    assert reverify_record.commands == ['grep -q "value = 1" existing.py']
    assert "the project's pinned verify_command" in reverify_record.detail
