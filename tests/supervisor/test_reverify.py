"""G10: the supervisor re-runs the worker's verification — claims are not trusted."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.reverify import ReverifyOutcome, reverify

from .conftest import git


def _no_leftovers(repo: Path, config: SupervisorConfig) -> None:
    worktrees = list(config.worktrees_root.iterdir()) if config.worktrees_root.is_dir() else []
    assert worktrees == [], f"leftover worktrees (re-verify did not clean up): {worktrees}"
    listed = git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, f"git still tracks extra worktrees:\n{listed}"


def _kept_as_evidence(repo: Path, config: SupervisorConfig, task_id: str) -> None:
    """v107-F1: an unconfirmed completed run keeps its tree for diagnosis."""
    worktrees = [p.name for p in config.worktrees_root.iterdir()]
    assert worktrees == [task_id], f"expected only the preserved tree: {worktrees}"


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


def test_reverify_gets_the_runs_local_provider_port_too(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F252 (D6, I2): the worker reached its local model provider's port
    (provider_hosts) beside the project's loopback_ports; G10 got only the
    latter, so a test needing the local provider failed in re-verify only."""
    from skep.supervisor import dispatch
    from skep.supervisor.reverify import reverify_run as real

    seen: dict[str, object] = {}

    def spy(**kwargs: object) -> ReverifyOutcome:
        seen.update(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dispatch, "reverify_run", spy)
    monkeypatch.setattr(dispatch, "provider_hosts_for", lambda *_a, **_k: ["localhost:11434"])
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"
    assert seen["loopback_ports"] == (11434,)


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
    # v115-F12: and the record says what the re-run SAID, not only its exit code.
    assert "--- re-run output (tail) ---" in reverify.detail
    assert "full output: " in reverify.detail
    output_path = Path(reverify.detail.split("full output: ", 1)[1].split("\n", 1)[0])
    assert output_path.is_file() and "(exit " in output_path.read_text(encoding="utf-8")
    _kept_as_evidence(repo, config, outcome.record.task_id)


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
    _kept_as_evidence(repo, config, outcome.record.task_id)


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


def test_a_wall_that_cannot_be_built_is_unavailable_not_a_crash(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F251: a profile the backend refuses (a privileged loopback port on
    bubblewrap) makes re-verification honestly unavailable, with the reason —
    never an exception out of G10 and never an unconfined re-run."""
    from skep.supervisor import sandbox

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise sandbox.SandboxUnavailableError("loopback port 80 is below 1024")

    monkeypatch.setattr(sandbox, "write_profile", refuse)
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["true"],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": "/usr/bin:/bin"},
    )
    assert outcome.outcome == "unavailable"
    assert outcome.exit_codes == []
    assert "loopback port 80 is below 1024" in outcome.detail


@pytest.mark.parametrize("fails", ["proxy_start", "proxy_stop"])
def test_reverify_removes_its_proxy_socket_dir_whatever_the_proxy_did(
    repo: Path,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fails: str,
) -> None:
    """ES-F257: the bubblewrap loopback bridge's socket dir (skep-nx-*) was
    removed only through a started proxy; a proxy that failed to start (its
    sockets bind in the constructor) or to stop left the dir behind — Fedora
    collected 124 under /run/user/1000. The dir now goes in the finally."""
    import importlib

    from skep.supervisor import dispatch, sandbox

    reverify_module = importlib.import_module("skep.supervisor.reverify")
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(dispatch, "_proxy_socket_root", lambda: str(root))
    bwrap = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: bwrap)
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **_kw: path)

    class Proxy:
        port = 40000

        def __init__(self, _allow: object, *, unix_socket_path: str, **_kw: object) -> None:
            if fails == "proxy_start":
                raise OSError("cannot bind the bridge socket")
            self.unix_socket_path = unix_socket_path

        def start(self) -> Proxy:
            return self

        def stop(self) -> None:
            raise RuntimeError("stop failed")

    monkeypatch.setattr(reverify_module, "FilteringProxy", Proxy)
    with pytest.raises((OSError, RuntimeError)):
        reverify_module.reverify(
            repo=repo,
            ref=None,
            patch_path=_patch_adding_marker(tmp_path),
            commands=["true"],
            config=config,
            profile_path=tmp_path / "reverify.profile.sb",
            env={"PATH": "/usr/bin:/bin"},
            loopback_ports=(5432,),
        )
    assert list(root.iterdir()) == []


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


def test_reverify_run_primes_against_the_shared_project_cache(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v109-F4: priming resolves into the per-project cache dispatch already
    uses — not a worktree-local throwaway that dies with the tree — and both
    reverify profiles keep that root writable."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pyproject")
    bin_dir = tmp_path / "uvbin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    # The stub logs INTO the cache dir itself: the log lands at the shared
    # root only if UV_CACHE_DIR points there AND the profiles let uv write it.
    stub.write_text(
        '#!/bin/sh\necho "$1 cache=${UV_CACHE_DIR:-unset}" >> "$UV_CACHE_DIR/uv.log"\nexit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    happy = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert happy.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        from skep.supervisor.policy_resolver import project_cache_root
        from skep.supervisor.reverify import reverify_run

        outcome = reverify_run(
            store=store,
            task_id=happy.record.task_id,
            worker_outcome="passed",
            repo=repo,
            ref=None,
            config=config,
            verify_command="uv run pytest",
        )
        shared = project_cache_root(store, config, repo) / "uv"
    finally:
        store.close()
    assert outcome.outcome == "passed", outcome.detail
    lines = (shared / "uv.log").read_text(encoding="utf-8").splitlines()
    # The prime ran first against the shared cache; the pinned command ran
    # second with the same cache. The throwaway never appears.
    assert lines[0].startswith("sync")
    assert lines[1].startswith("run")
    assert f"cache={shared}" in lines[0]
    assert f"cache={shared}" in lines[1]
    assert ".uv-cache" not in "\n".join(lines)


def test_reverify_timeout_is_the_budget_floored_at_the_default() -> None:
    """The flat 300s cap timed out a healthy 10-minute pinned suite (dogfood
    019fc72c, exit -1) — the re-run is afforded the run's own budget, and a
    small budget never tightens G10 below the historic default."""
    from skep.supervisor.reverify import _REVERIFY_TIMEOUT_SECONDS, command_timeout_seconds

    assert command_timeout_seconds(None) == _REVERIFY_TIMEOUT_SECONDS
    assert command_timeout_seconds(60.0) == _REVERIFY_TIMEOUT_SECONDS
    assert command_timeout_seconds(6000.0) == 6000.0


def test_reverify_env_carries_a_worktree_local_tmpdir(repo: Path, config: SupervisorConfig) -> None:
    """v107-F3: the re-run's TMPDIR lives inside its own worktree — unset it
    fell back to /tmp, which a nested bwrap (skep's own suite) tmpfs-masks."""
    happy = run_task(repo, "Fix the bug. MODE:happy", config=config)
    store = RunStore(config.db_path)
    try:
        from skep.supervisor.reverify import reverify_run

        reverify_run(
            store=store,
            task_id=happy.record.task_id,
            worker_outcome="passed",
            repo=repo,
            ref=None,
            config=config,
            verify_command=(
                'sh -c \'case "$TMPDIR" in *".reverify-tmp") ;; *) exit 7;; esac; '
                'case "$TMPPREFIX" in "$TMPDIR"/zsh) ;; *) exit 9;; esac; '
                'case "$CLANG_MODULE_CACHE_PATH" in "$TMPDIR"/*) exit 0;; *) exit 8;; esac\''
            ),
        )
        record = store.reverification_for(happy.record.task_id)
    finally:
        store.close()
    assert record is not None
    assert record.exit_codes == [0], record.detail


def _lock_bump_patch(tmp_path: Path) -> Path:
    patch = tmp_path / "lock.patch"
    patch.write_text(
        "diff --git a/uv.lock b/uv.lock\n"
        "--- a/uv.lock\n"
        "+++ b/uv.lock\n"
        "@@ -1,4 +1,4 @@\n"
        " version = 1\n"
        " [[package]]\n"
        ' name = "dep"\n'
        '-version = "1.0"\n'
        '+version = "2.0"\n'
        "diff --git a/patched_marker.txt b/patched_marker.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/patched_marker.txt\n"
        "@@ -0,0 +1 @@\n"
        "+patched\n",
        encoding="utf-8",
    )
    return patch


def _uv_project(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    (repo / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "dep"\nversion = "1.0"\n', encoding="utf-8"
    )
    git(repo, "add", "pyproject.toml", "uv.lock")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "uv project")


def test_a_lockfile_patch_is_primed_again_after_it_applies(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v115-F13 (I2, run 01a16157): the baseline prime installed the OLD
    dependencies and the pinned command then ran offline against them — a
    uv.lock bump of starlette 1.3.0 → 1.9.0 was "re-verified" on 1.3.0. A
    patch that changes the lockfile is primed a second time, after it lands,
    before the offline verify."""
    _uv_project(repo)
    bin_dir, log = _uv_stub(tmp_path)
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_lock_bump_patch(tmp_path),
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )
    assert outcome.outcome == "passed", outcome.detail
    assert "re-primed against the patched lockfile" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("sync nosync=unset") and "patched=no" in lines[0]
    assert lines[1].startswith("sync nosync=unset") and "patched=yes" in lines[1]
    assert lines[2].startswith("run nosync=1") and "patched=yes" in lines[2]


def test_a_lockfile_patch_that_cannot_be_primed_is_unavailable_not_passed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """The second prime fails (the new package is not in the cache and there is
    no network): the verify never runs and the verdict is unavailable — stale
    dependencies are never a confirmation."""
    _uv_project(repo)
    bin_dir, log = _uv_stub(tmp_path)
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        'patched="no"\n'
        '[ -e patched_marker.txt ] && patched="yes"\n'
        'echo "$1 nosync=${UV_NO_SYNC:-unset} patched=$patched" >> "$UV_LOG"\n'
        'if [ "$1" = sync ] && [ "$patched" = yes ]; then'
        ' echo "no wheel for dep 2.0" >&2; exit 7; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_lock_bump_patch(tmp_path),
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )
    assert outcome.outcome == "unavailable"
    assert outcome.exit_codes == [7]
    assert "never confirmed against the old dependencies" in outcome.detail
    assert "no wheel for dep 2.0" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [line.split()[0] for line in lines] == ["sync", "sync"]  # the verify never ran


def test_es_f3_landing_builds_on_the_exact_base_g10_verified(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F3 (B131, I1 I2): main moved after a confirmed re-verify, and the
    landing built on the NEW tip — a NameError landed labelled "confirmed".
    The re-verify pins the commit it ran on; landing builds on exactly it."""

    from skep.supervisor.serve import actions

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    verified_on = git(repo, "rev-parse", "main").stdout.strip()
    (repo / "moved.py").write_text("x = 1\n")
    git(repo, "add", "moved.py")
    git(repo, "commit", "-qm", "main moved after re-verify")
    task_id = outcome.record.task_id

    store = RunStore(config.db_path)
    try:
        record = store.reverification_for(task_id)
        assert record is not None and record.confirmed
        assert record.base_commit == verified_on
        landed = actions.land_run(store, actions.require_run(store, task_id), "tester")
        assert "warning" not in landed
        # Appending past the verified commit (a grouped stack) still lands, but
        # nothing calls the landed tree confirmed any more.
        git(repo, "branch", "elsewhere", "main")
        stacked = run_task(repo, "Fix the bug. MODE:happy", config=config).record.task_id
        git(repo, "commit", "-q", "--allow-empty", "-m", "moved again")
        git(repo, "branch", "-f", "elsewhere", "main")
        out = actions.land_run(store, actions.require_run(store, stacked), "t", branch="elsewhere")
        assert "never re-verified" in out["warning"]
        assert store.reverification_for(stacked).confirmed is False  # type: ignore[union-attr]
    finally:
        store.close()
    parent = git(repo, "rev-parse", f"{landed['branch']}^").stdout.strip()
    assert parent == verified_on


# --- v127-A0: G10 confirms Node patches -------------------------------------


def _node_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Fake `npm`/`pnpm`/`yarn`/`node` that log each call (tool, argv, whether the
    patch had landed, the npm cache) — no network, like `_uv_stub`."""
    bin_dir = tmp_path / "nodebin"
    bin_dir.mkdir()
    log = tmp_path / "node.log"
    for tool in ("npm", "pnpm", "yarn", "node"):
        stub = bin_dir / tool
        stub.write_text(
            "#!/bin/sh\n"
            'patched="no"\n'
            '[ -e patched_marker.txt ] && patched="yes"\n'
            f'echo "{tool} $* patched=$patched cache=${{npm_config_cache:-unset}}'
            " proxy=${HTTPS_PROXY:-unset} corepack=${COREPACK_ENABLE_NETWORK:-unset}"
            f' logs=${{npm_config_logs_dir:-unset}}" >> "$NODE_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return bin_dir, log


def _npm_lock(version: str, *, host: str = "registry.npmjs.org", integrity: bool = True) -> str:
    """A one-line npm v3 lockfile pinning jest@version (resolved + integrity)."""
    entry = {"version": version, "resolved": f"https://{host}/jest/-/jest-{version}.tgz"}
    if integrity:
        entry["integrity"] = "sha512-abc"
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {"": {"devDependencies": {"jest": version}}, "node_modules/jest": entry},
        }
    )


def _node_project(repo: Path, lockfile: str | None = "package-lock.json") -> None:
    (repo / "package.json").write_text(
        '{"name": "x", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    files = ["package.json"]
    if lockfile is not None:
        (repo / lockfile).write_text(_npm_lock("1.0.0") + "\n", encoding="utf-8")
        files.append(lockfile)
    git(repo, "add", *files)
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "node project")


def _node_lock_bump_patch(
    tmp_path: Path, *, extra: str = "", lock: str = "", old: str = ""
) -> Path:
    patch = tmp_path / "node-lock.patch"
    patch.write_text(
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1 +1 @@\n"
        f"-{old or _npm_lock('1.0.0')}\n"
        f"+{lock or _npm_lock('2.0.0')}\n"
        "diff --git a/patched_marker.txt b/patched_marker.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/patched_marker.txt\n"
        "@@ -0,0 +1 @@\n"
        "+patched\n" + extra,
        encoding="utf-8",
    )
    return patch


def _node_reverify(
    repo: Path, config: SupervisorConfig, patch: Path, bin_dir: Path, log: Path, pin: str
) -> ReverifyOutcome:
    return reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=[pin],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "NODE_LOG": str(log)},
    )


@pytest.mark.parametrize(
    ("lockfile", "pin", "install"),
    [
        ("package-lock.json", "npm test", "npm ci --ignore-scripts"),
        (
            "pnpm-lock.yaml",
            "pnpm test",
            "pnpm install --frozen-lockfile --ignore-scripts --ignore-pnpmfile",
        ),
        ("yarn.lock", "yarn test", "yarn install --frozen-lockfile --ignore-scripts"),
    ],
)
def test_a_node_pin_primes_from_the_baseline_lockfile_then_verifies_offline(
    repo: Path, config: SupervisorConfig, tmp_path: Path, lockfile: str, pin: str, install: str
) -> None:
    """v127-A0 (O12, I2): G10 re-ran `npm test` in a fresh worktree with no
    node_modules, so every Node patch read NOT CONFIRMED. The baseline
    lockfile is installed first — lifecycle scripts off, before the patch
    lands — then the pin runs as before."""
    _node_project(repo, lockfile)
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "passed", outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith(f"{install} ") and "patched=no" in lines[0]
    assert "cache=unset" not in lines[0]  # never the operator's ~/.npm
    assert lines[1].startswith(f"{pin} ") and "patched=yes" in lines[1]
    assert "primed from the baseline" in outcome.detail
    assert len(lines) == 2  # no lockfile change, no second install


def test_a_bare_node_pin_in_a_repo_with_dependencies_is_primed_too(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v127-A0 review: `node --test tests/*.test.mjs` is the pin setup and the
    trial repos use; its tests import the repo's dependencies just like
    `npm test` does, so it primes from the baseline lockfile the same way."""
    _node_project(repo, "package-lock.json")
    bin_dir, log = _node_stubs(tmp_path)
    pin = "node --test tests/a.test.mjs"
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "passed", outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("npm ci --ignore-scripts ") and "patched=no" in lines[0]
    assert lines[1].startswith(f"{pin} ") and "patched=yes" in lines[1]


def test_a_node_lockfile_patch_is_primed_again_after_it_applies(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v127-A0, the v115-F13 twin: a patch that bumps the lockfile is verified
    against the NEW packages, not the baseline's."""
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(
        repo, config, _node_lock_bump_patch(tmp_path), bin_dir, log, "npm test"
    )
    assert outcome.outcome == "passed", outcome.detail
    assert "re-primed against the patched lockfile" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("npm ci --ignore-scripts ") and "patched=no" in lines[0]
    assert lines[1].startswith("npm ci --ignore-scripts ") and "patched=yes" in lines[1]
    assert lines[2].startswith("npm test ") and "patched=yes" in lines[2]


def test_a_node_lockfile_patch_that_also_edits_installer_config_is_not_reprimed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """The re-prime has the network. `--ignore-scripts` keeps lifecycle scripts
    off, but a patched `.yarnrc.yml` (yarnPath) or `.npmrc` would run patch
    code or reroute the install anyway — so that patch is not re-primed and
    is never confirmed against the old packages either (I2, I12)."""
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    npmrc = (
        "diff --git a/.npmrc b/.npmrc\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.npmrc\n"
        "@@ -0,0 +1 @@\n"
        "+ignore-scripts=false\n"
    )
    outcome = _node_reverify(
        repo, config, _node_lock_bump_patch(tmp_path, extra=npmrc), bin_dir, log, "npm test"
    )
    assert outcome.outcome == "unavailable"
    assert ".npmrc" in outcome.detail and "was not run" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "patched=no" in lines[0]  # baseline prime only


def test_a_node_repo_with_dependencies_and_no_lockfile_is_not_confirmed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """No lockfile, nothing to install from deterministically: the truthful
    verdict, not a crash and not a re-run against missing node_modules."""
    _node_project(repo, lockfile=None)
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, "npm test")
    assert outcome.outcome == "unavailable"
    assert "not confirmed: no lockfile to prime from" in outcome.detail
    assert not log.exists()  # neither an install nor the pin ran


def test_a_dependency_free_node_repo_runs_its_pin_unprimed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """A package.json with no dependencies has nothing to prime — the pin runs
    exactly as it did before A0."""
    (repo / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    git(repo, "add", "package.json")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bare node")
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, "npm test")
    assert outcome.outcome == "passed", outcome.detail
    assert log.read_text(encoding="utf-8").splitlines()[0].startswith("npm test ")


def test_a_failed_node_prime_is_unavailable_not_patch_guilt(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    (bin_dir / "npm").write_text("#!/bin/sh\necho 'registry down' >&2\nexit 9\n", encoding="utf-8")
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, "npm test")
    assert outcome.outcome == "unavailable"
    assert outcome.exit_codes == [9]
    assert "npm ci --ignore-scripts" in outcome.detail and "was not run" in outcome.detail


def test_a_node_lockfile_patch_that_cannot_be_primed_is_unavailable_not_passed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    (bin_dir / "npm").write_text(
        "#!/bin/sh\n"
        'echo "npm $*" >> "$NODE_LOG"\n'
        'if [ -e patched_marker.txt ]; then echo "no tarball for jest 2.0.0" >&2; exit 7; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    outcome = _node_reverify(
        repo, config, _node_lock_bump_patch(tmp_path), bin_dir, log, "npm test"
    )
    assert outcome.outcome == "unavailable"
    assert outcome.exit_codes == [7]
    assert "never confirmed against the old dependencies" in outcome.detail
    assert "no tarball for jest 2.0.0" in outcome.detail
    assert [line.split()[1] for line in log.read_text(encoding="utf-8").splitlines()] == [
        "ci",
        "ci",
    ]  # the pin never ran


# --- v127-A0 review (council round 2): the prime's network is the registry ---


def _new_file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


class _RecordingWall:
    """Stand-ins for the sandbox and proxy: record every profile and proxy the
    re-verify builds, run the commands unwrapped (no real wall in tests)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        from skep.supervisor import sandbox

        self.profiles: dict[str, dict[str, object]] = {}
        self.proxies: list[tuple[str, ...]] = []
        module = importlib.import_module("skep.supervisor.reverify")
        wall = self

        def write_profile(path: Path, **kwargs: object) -> Path:
            wall.profiles[path.name] = kwargs
            return path

        class Proxy:
            port = 40123

            def __init__(
                self, allow: tuple[str, ...], *, unix_socket_path: str | None = None, **_kw: object
            ) -> None:
                wall.proxies.append(tuple(allow))
                self.unix_socket_path = unix_socket_path

            def start(self) -> Proxy:
                return self

            def stop(self) -> None:
                pass

        seatbelt = sandbox.SandboxAvailability(True, backend="seatbelt")
        monkeypatch.setattr(sandbox, "availability", lambda backend=None: seatbelt)
        monkeypatch.setattr(sandbox, "write_profile", write_profile)
        monkeypatch.setattr(sandbox, "wrap_command", lambda argv, _profile, **_kw: list(argv))
        monkeypatch.setattr(module, "FilteringProxy", Proxy)


def test_the_node_prime_reaches_only_the_baseline_registry_hosts(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Council round 2 (HIGH, I12/I2/I11): the prime and the re-prime ran under
    ALLOW_ALL_NETWORK, so a patched lockfile's `resolved` URLs reached any
    host before approval. Both now go through the filtering proxy, which
    admits the default registries plus the hosts the BASELINE lockfile
    resolves from — never allow-all."""
    from skep.supervisor import sandbox

    _node_project(repo)
    (repo / "package-lock.json").write_text(
        _npm_lock("1.0.0", host="npm.example.com") + "\n", encoding="utf-8"
    )
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "own registry")
    wall = _RecordingWall(monkeypatch)
    bin_dir, log = _node_stubs(tmp_path)
    patch = _node_lock_bump_patch(
        tmp_path,
        old=_npm_lock("1.0.0", host="npm.example.com"),
        lock=_npm_lock("2.0.0", host="npm.example.com"),
    )
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=["npm test"],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "NODE_LOG": str(log)},
    )
    assert outcome.outcome == "passed", outcome.detail
    hosts = ("npm.example.com", "registry.npmjs.org", "registry.yarnpkg.com")
    assert wall.proxies == [hosts]
    prime = wall.profiles["reverify.profile.prime.sb"]
    assert prime["network"] == sandbox.NetworkPolicy(allow=hosts)
    assert prime["proxy_port"] == 40123
    lines = log.read_text(encoding="utf-8").splitlines()
    # both installs went through the proxy with corepack offline; the pin did not
    for line in lines[:2]:
        assert line.startswith("npm ci ") and "proxy=http://127.0.0.1:40123" in line
        assert "corepack=0" in line
    assert lines[2].startswith("npm test ") and "proxy=unset" in lines[2]


def test_the_uv_prime_reaches_only_the_baseline_index_hosts(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uv twin: pypi.org, files.pythonhosted.org and the indexes the
    baseline uv.lock names — never allow-all."""
    from skep.supervisor import sandbox

    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    (repo / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "dep"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.example.org/simple" }\n',
        encoding="utf-8",
    )
    git(repo, "add", "pyproject.toml", "uv.lock")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "uv project")
    wall = _RecordingWall(monkeypatch)
    bin_dir, log = _uv_stub(tmp_path)
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["uv run pytest"],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )
    assert outcome.outcome == "passed", outcome.detail
    hosts = ("files.pythonhosted.org", "pypi.example.org", "pypi.org")
    assert wall.proxies == [hosts]
    assert wall.profiles["reverify.profile.prime.sb"]["network"] == sandbox.NetworkPolicy(
        allow=hosts
    )


def test_the_verify_profile_keeps_the_node_cache_read_only(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Council round 2 (LOW, I2): patch code ran offline with the shared Node
    cache writable, so a never-landed patch could poison later verdicts. The
    verify profile reads it only; npm's logs go to the re-verify tmp dir."""
    _node_project(repo)
    wall = _RecordingWall(monkeypatch)
    bin_dir, log = _node_stubs(tmp_path)
    node_cache = tmp_path / "cache" / "node"
    uv_cache = tmp_path / "cache" / "uv"
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["npm test"],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "NODE_LOG": str(log)},
        node_cache_dir=node_cache,
        uv_cache_dir=uv_cache,
    )
    assert outcome.outcome == "passed", outcome.detail
    verify = wall.profiles["reverify.profile.sb"]
    assert node_cache not in list(verify["extra_writable"])  # type: ignore[call-overload]
    assert uv_cache in list(verify["extra_writable"])  # type: ignore[call-overload]
    assert node_cache in list(verify["extra_readable"])  # type: ignore[call-overload]
    prime = wall.profiles["reverify.profile.prime.sb"]
    assert node_cache in list(prime["extra_writable"])  # type: ignore[call-overload]
    pin_line = log.read_text(encoding="utf-8").splitlines()[1]
    assert pin_line.startswith("npm test ") and ".reverify-tmp" in pin_line.split("logs=")[1]


@pytest.mark.parametrize(
    ("lock", "extra", "reason"),
    [
        (_npm_lock("2.0.0", host="evil.example.com"), "", "evil.example.com"),
        (_npm_lock("2.0.0", integrity=False), "", "integrity"),
        (
            _npm_lock("2.0.0").replace(
                "https://registry.npmjs.org/jest/-/jest-2.0.0.tgz",
                "git+ssh://git@github.com/x/jest.git#abc",
            ),
            "",
            "git+ssh://",
        ),
        ("", _new_file_diff("pnpm-workspace.yaml", "packages: ['../*']"), "pnpm-workspace.yaml"),
        ("", _new_file_diff("sub/.yarnrc", 'yarn-path "../x.js"'), "sub/.yarnrc"),
        ("", _new_file_diff(".pnpmfile.mjs", "export default {}"), ".pnpmfile.mjs"),
    ],
)
def test_a_patched_lockfile_off_the_registry_is_never_installed(
    repo: Path, config: SupervisorConfig, tmp_path: Path, lock: str, extra: str, reason: str
) -> None:
    """Council round 2 (HIGH): before the re-prime, the patched lockfile must
    resolve every package from the baseline's registry hosts with an integrity
    hash, and the patch must leave every installer config alone — otherwise
    nothing is installed and the verdict is unavailable (with the reason)."""
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    patch = _node_lock_bump_patch(tmp_path, lock=lock, extra=extra)
    outcome = _node_reverify(repo, config, patch, bin_dir, log, "npm test")
    assert outcome.outcome == "unavailable", outcome.detail
    assert reason in outcome.detail and "was not run" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "patched=no" in lines[0]  # the baseline prime only


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        ('{"name": "x", "devDependencies": {"jest": "github:evil/jest"}}', "github:evil/jest"),
        ('{"name": "x", "devDependencies": {"jest": "evil/jest#main"}}', "evil/jest#main"),
        ('{"name": "x", "devDependencies": {"jest": "file:../outside"}}', "file:../outside"),
        (
            '{"name": "x", "devDependencies": {"jest": "1.0.0"}, '
            '"overrides": {"jest": {"dep": "https://evil.example.com/d.tgz"}}}',
            "https://evil.example.com/d.tgz",
        ),
        (
            '{"name": "x", "devDependencies": {"jest": "1.0.0"}, "workspaces": ["../up"]}',
            "../up",
        ),
        (
            '{"name": "x", "devDependencies": {"jest": "1.0.0"}, "packageManager": "yarn@4.0.0"}',
            "packageManager",
        ),
    ],
)
def test_a_patched_manifest_off_the_registry_is_never_installed(
    repo: Path, config: SupervisorConfig, tmp_path: Path, manifest: str, reason: str
) -> None:
    """git/github:/file:/URL specs, a workspace outside the tree and a changed
    `packageManager` (corepack would fetch and run it) all refuse the re-prime."""
    _node_project(repo)
    bin_dir, log = _node_stubs(tmp_path)
    patch = tmp_path / "manifest.patch"
    patch.write_text(
        "diff --git a/package.json b/package.json\n"
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -1 +1 @@\n"
        '-{"name": "x", "devDependencies": {"jest": "1.0.0"}}\n'
        f"+{manifest}\n",
        encoding="utf-8",
    )
    outcome = _node_reverify(repo, config, patch, bin_dir, log, "npm test")
    assert outcome.outcome == "unavailable", outcome.detail
    assert reason in outcome.detail and "was not run" in outcome.detail
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_a_patch_to_the_baseline_yarn_path_target_is_never_installed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """yarn v1 runs the file a `.yarnrc` `yarn-path` names — outside `.yarn/`
    a patch could rewrite it and the re-prime would run it with the network."""
    _node_project(repo, "yarn.lock")
    (repo / ".yarnrc").write_text('yarn-path "tools/yarn.js"\n', encoding="utf-8")
    git(repo, "add", ".yarnrc")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "yarnrc")
    bin_dir, log = _node_stubs(tmp_path)
    patch = tmp_path / "yarn-path.patch"
    patch.write_text(
        "diff --git a/yarn.lock b/yarn.lock\n"
        "--- a/yarn.lock\n"
        "+++ b/yarn.lock\n"
        "@@ -1 +1 @@\n"
        f"-{_npm_lock('1.0.0')}\n"
        f"+{_npm_lock('2.0.0')}\n" + _new_file_diff("tools/yarn.js", "fetch('https://x')"),
        encoding="utf-8",
    )
    outcome = _node_reverify(repo, config, patch, bin_dir, log, "yarn test")
    assert outcome.outcome == "unavailable", outcome.detail
    assert "tools/yarn.js" in outcome.detail
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_a_bare_node_pin_without_a_lockfile_runs_unprimed_and_says_so(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """Council round 2: the bare-`node` trigger made `node --test` in a repo
    with dependencies but no lockfile unavailable forever. It runs unprimed
    and the record says why; npm/pnpm/yarn pins keep the refusal."""
    _node_project(repo, lockfile=None)
    bin_dir, log = _node_stubs(tmp_path)
    pin = "node --test tests/a.test.mjs"
    outcome = _node_reverify(repo, config, _patch_adding_marker(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "passed", outcome.detail
    assert "unprimed" in outcome.detail and "no lockfile" in outcome.detail
    assert log.read_text(encoding="utf-8").splitlines()[0].startswith(f"{pin} ")


def _first_deps_patch(tmp_path: Path, *, lockfile: bool) -> Path:
    patch = tmp_path / "first-deps.patch"
    body = (
        "diff --git a/package.json b/package.json\n"
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -1 +1 @@\n"
        '-{"name": "x"}\n'
        '+{"name": "x", "devDependencies": {"jest": "1.0.0"}}\n'
    )
    if lockfile:
        body += _new_file_diff("package-lock.json", _npm_lock("1.0.0"))
    patch.write_text(body + _new_file_diff("patched_marker.txt", "patched"), encoding="utf-8")
    return patch


def _dependency_free_node_repo(repo: Path) -> None:
    (repo / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    git(repo, "add", "package.json")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bare node")


def test_a_patch_adding_the_first_dependencies_is_primed_from_its_lockfile(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """Council round 2: the baseline had nothing to prime, so a patch adding
    the first dependencies and a lockfile ran with no node_modules and was
    recorded FAILED. It is a lockfile change: validated, then primed."""
    _dependency_free_node_repo(repo)
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(
        repo, config, _first_deps_patch(tmp_path, lockfile=True), bin_dir, log, "npm test"
    )
    assert outcome.outcome == "passed", outcome.detail
    assert "primed against the patched lockfile" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("npm ci --ignore-scripts ") and "patched=yes" in lines[0]
    assert lines[1].startswith("npm test ")


def test_a_patch_adding_dependencies_without_a_lockfile_is_unavailable_not_failed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _dependency_free_node_repo(repo)
    bin_dir, log = _node_stubs(tmp_path)
    outcome = _node_reverify(
        repo, config, _first_deps_patch(tmp_path, lockfile=False), bin_dir, log, "npm test"
    )
    assert outcome.outcome == "unavailable", outcome.detail
    assert "no lockfile" in outcome.detail and "was not run" in outcome.detail
    assert not log.exists()


def test_a_patch_adding_a_new_uv_lock_is_primed_against_it(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """One helper for "what did the patch touch": `git diff` missed a NEW
    (untracked) uv.lock, so a patch that adds the first lockfile was verified
    against the baseline's resolution."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pyproject")
    bin_dir, log = _uv_stub(tmp_path)
    patch = tmp_path / "new-lock.patch"
    patch.write_text(
        _new_file_diff("uv.lock", 'version = 1\n[[package]]\nname = "dep"')
        + _new_file_diff("patched_marker.txt", "patched"),
        encoding="utf-8",
    )
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV_LOG": str(log)},
    )
    assert outcome.outcome == "passed", outcome.detail
    assert "re-primed against the patched lockfile" in outcome.detail
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [line.split()[0] for line in lines] == ["sync", "sync", "run"]


_PNPM_OK = """lockfileVersion: '9.0'
importers:
  .:
    devDependencies:
      jest:
        specifier: ^1.0.0
        version: 1.0.0
      local:
        specifier: link:packages/local
        version: link:packages/local
packages:
  jest@1.0.0:
    resolution: {integrity: sha512-abc}
  other@1.0.0:
    resolution:
      integrity: sha512-def
      tarball: https://registry.npmjs.org/other/-/other-1.0.0.tgz
"""
_YARN_OK = """# yarn lockfile v1


"@scope/jest@^1.0.0", "@scope/jest@^1.1.0":
  version "1.1.0"
  resolved "https://registry.yarnpkg.com/@scope/jest/-/jest-1.1.0.tgz#abc"
  integrity sha512-abc
"""


@pytest.mark.parametrize(
    ("lockfile", "text", "reason"),
    [
        ("pnpm-lock.yaml", _PNPM_OK, ""),
        (
            "pnpm-lock.yaml",
            _PNPM_OK.replace(
                "{integrity: sha512-abc}", "{commit: a, repo: https://github.com/x/y, type: git}"
            ),
            "integrity",
        ),
        (
            "pnpm-lock.yaml",
            _PNPM_OK.replace("https://registry.npmjs.org/other", "https://evil.example.com/other"),
            "evil.example.com",
        ),
        (
            "pnpm-lock.yaml",
            _PNPM_OK.replace("version: link:packages/local", "version: link:../outside"),
            "link:../outside",
        ),
        (
            "pnpm-lock.yaml",
            _PNPM_OK.replace("specifier: ^1.0.0", "specifier: github:evil/jest"),
            "github:evil/jest",
        ),
        ("yarn.lock", _YARN_OK, ""),
        (
            "yarn.lock",
            _YARN_OK.replace('"@scope/jest@^1.1.0"', '"@scope/jest@github:evil/jest"'),
            "github:evil/jest",
        ),
        (
            "yarn.lock",
            _YARN_OK.replace("registry.yarnpkg.com", "evil.example.com"),
            "evil.example.com",
        ),
        (
            "package-lock.json",
            json.dumps(
                {
                    "lockfileVersion": 1,
                    "dependencies": {
                        "a": {
                            "version": "1.0.0",
                            "dependencies": {"b": {"version": "git+https://github.com/x/b.git"}},
                        }
                    },
                }
            ),
            "git+https://github.com/x/b.git",
        ),
        (
            "package-lock.json",
            json.dumps(
                {
                    "packages": {
                        "node_modules/w": {"resolved": "packages/w", "link": True},
                        "node_modules/z": {"inBundle": True},
                    }
                }
            ),
            "",
        ),
        (
            "package-lock.json",
            json.dumps({"packages": {"node_modules/w": {"resolved": "../outside", "link": True}}}),
            "../outside",
        ),
        ("package-lock.json", "not json", "could not be read"),
    ],
)
def test_the_patched_lockfile_check_reads_every_format(
    tmp_path: Path, lockfile: str, text: str, reason: str
) -> None:
    from skep.supervisor.reverify import _NPM_HOSTS, _node_install_refusal

    (tmp_path / lockfile).write_text(text, encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"workspaces": {"packages": ["packages/*"]}, "devDependencies": {"jest": "^1.0.0"}}',
        encoding="utf-8",
    )
    refusal = _node_install_refusal(
        tmp_path, lockfile, {lockfile}, hosts=_NPM_HOSTS, package_manager=None, protected=set()
    )
    assert (reason in refusal) if reason else refusal == "", refusal


def test_the_prime_wall_bridges_and_cleans_up_on_bubblewrap(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On bubblewrap the prime's proxy also listens on a unix socket the netns
    reaches through netshim; its directory is removed afterwards (ES-F257)."""
    from skep.supervisor import dispatch, sandbox

    _node_project(repo)
    wall = _RecordingWall(monkeypatch)
    bwrap = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: bwrap)
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(dispatch, "_proxy_socket_root", lambda: str(root))
    bin_dir, log = _node_stubs(tmp_path)
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_patch_adding_marker(tmp_path),
        commands=["npm test"],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "NODE_LOG": str(log)},
    )
    assert outcome.outcome == "passed", outcome.detail
    socket = wall.profiles["reverify.profile.prime.sb"]["unix_socket_path"]
    assert isinstance(socket, str) and socket.startswith(str(root))
    assert list(root.iterdir()) == []
