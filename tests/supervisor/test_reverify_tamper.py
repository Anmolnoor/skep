"""V1 (phase-01 1.3): G10's test-tamper guard. A patch that makes the pinned
check pass by changing the tests — deleting, emptying or skipping them,
deselecting them from a conftest hook, or quietly removing a failing test —
is not confirmed and never auto-lands. Real throwaway repos, a real pytest
pin, no mock of the diff or of pytest."""

from __future__ import annotations

import importlib
import json
import shlex
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.policy import VERIFIED_PATCH_RULE, maybe_auto_approve
from skep.supervisor.reverify import ReverifyOutcome, pytest_counts, reverify_run

from .conftest import git, serve_client

# skep.supervisor re-exports the reverify() function under the module's name.
reverify_mod = importlib.import_module("skep.supervisor.reverify")

PIN = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"

PYPROJECT = (
    "[project]\n"
    'name = "toy"\n'
    'version = "0"\n'
    'dependencies = ["requests>=2.0"]\n'
    "\n"
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n'
)
TEST_A = "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n"
TEST_B = "def test_three():\n    assert 3 == 3\n"
FAILING = "\n\ndef test_broken():\n    assert 1 == 2\n"


def _seed(repo: Path, *, failing: bool = False) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text(TEST_A + (FAILING if failing else ""))
    (repo / "tests" / "test_b.py").write_text(TEST_B)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tests")


def _patch(repo: Path, name: str, edit: Callable[[Path], object]) -> Path:
    edit(repo)
    git(repo, "add", "-A")
    text = git(
        repo, "diff", "--cached", "--binary", "--no-color", "--src-prefix=a/", "--dst-prefix=b/"
    ).stdout
    git(repo, "reset", "-q", "--hard", "HEAD")
    path = repo.parent / f"{name}.patch"
    path.write_text(text, encoding="utf-8")
    return path


def _run(
    repo: Path, config: SupervisorConfig, patch: Path, pin: str = PIN
) -> tuple[ReverifyOutcome, Any, Any, dict[str, str]]:
    """G10 on ``patch`` exactly as dispatch runs it, then the landing lane."""
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo.parent / f"ws-{patch.stem}", instructions="v1")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed", None)
        store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
        outcome = reverify_run(
            store=store,
            task_id=task.task_id,
            worker_outcome="passed",
            repo=repo,
            ref=None,
            config=config,
            verify_command=pin,
        )
        record = store.reverification_for(task.task_id)
        approved = maybe_auto_approve(
            store=store,
            rules=(VERIFIED_PATCH_RULE,),
            repo=repo,
            task_id=task.task_id,
            verification_outcome="passed",
            risk_flags=(),
            verify_pinned=True,
        )
        blocked: dict[str, str] = {}
        for _, detail, _ in store.transitions_for(task.task_id):
            if detail and "auto_apply_blocked" in detail:
                blocked = json.loads(detail)["auto_apply_blocked"]
    finally:
        store.close()
    return outcome, record, approved, blocked


def _assert_tampered(result: tuple[ReverifyOutcome, Any, Any, dict[str, str]], *flags: str) -> None:
    outcome, record, approved, blocked = result
    assert outcome.outcome == "tampered", outcome.detail
    for flag in flags:
        assert flag in outcome.tamper_flags, outcome.tamper_flags
    # Persisted, not just returned (I8): the record every surface reads.
    assert record.outcome == "tampered"
    assert record.confirmed is False
    assert list(record.tamper_flags) == list(outcome.tamper_flags)
    assert "cannot prove an edited assertion" in record.detail
    # The landing lane refuses and says why (v106-F11 path).
    assert approved is None
    assert "test-tamper" in blocked[VERIFIED_PATCH_RULE.name]


# --- the four lying-agent shapes ------------------------------------------------


def test_a_deleted_test_file_is_tampered(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    patch = _patch(repo, "delete", lambda r: (r / "tests" / "test_b.py").unlink())
    result = _run(repo, config, patch)
    _assert_tampered(result, "test_file_deleted:tests/test_b.py", "test_total_dropped")
    counts = result[0].test_counts
    assert counts is not None and counts["status"] == "ok"
    assert counts["baseline"]["total_executed"] == 3
    assert counts["patched"]["total_executed"] == 2


def test_an_added_skip_marker_is_tampered(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)

    def skip(r: Path) -> None:
        (r / "tests" / "test_a.py").write_text(
            "import pytest\n\n\n"
            + TEST_A.replace("def test_two", "@pytest.mark.skip\ndef test_two")
        )

    result = _run(repo, config, _patch(repo, "skip", skip))
    _assert_tampered(
        result,
        "skip_marker_added:tests/test_a.py",
        "test_skipped_increased",
        "test_passed_dropped",
    )


def test_a_conftest_deselection_hook_is_tampered(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)

    def hook(r: Path) -> None:
        (r / "tests" / "conftest.py").write_text(
            "def pytest_collection_modifyitems(config, items):\n"
            "    items[:] = [i for i in items if i.name != 'test_two']\n"
        )

    result = _run(repo, config, _patch(repo, "hook", hook))
    _assert_tampered(result, "test_config_changed:tests/conftest.py", "test_total_dropped")


def test_deleting_one_failing_test_drops_the_executed_total(
    repo: Path, config: SupervisorConfig
) -> None:
    """No file deleted, no marker added, ``passed`` unchanged: only the
    executed total says a test vanished (the shape counts exist for)."""
    _seed(repo, failing=True)
    patch = _patch(repo, "drop", lambda r: (r / "tests" / "test_a.py").write_text(TEST_A))
    result = _run(repo, config, patch)
    _assert_tampered(result, "test_total_dropped")
    outcome = result[0]
    assert outcome.tamper_flags == ("test_total_dropped",)
    counts = outcome.test_counts
    assert counts is not None
    assert counts["baseline"]["passed"] == counts["patched"]["passed"] == 3
    assert counts["baseline"]["failed"] == 1
    # The re-run itself was clean — the tamper verdict is what blocks it.
    assert outcome.exit_codes == [0]


# --- what V1 must NOT flag, and how it fails closed ----------------------------


def test_a_dependency_only_pyproject_bump_is_not_flagged(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    patch = _patch(
        repo,
        "bump",
        lambda r: (r / "pyproject.toml").write_text(PYPROJECT.replace(">=2.0", ">=2.32")),
    )
    outcome, record, approved, blocked = _run(repo, config, patch)
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.tamper_flags == ()
    assert record.confirmed is True
    assert record.test_counts["status"] == "ok"
    assert approved is not None, blocked


def test_a_pytest_addopts_deselect_edit_is_flagged(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    patch = _patch(
        repo,
        "addopts",
        lambda r: (r / "pyproject.toml").write_text(
            PYPROJECT + 'addopts = "--deselect tests/test_b.py::test_three"\n'
        ),
    )
    _assert_tampered(_run(repo, config, patch), "test_config_changed:pyproject.toml")


def test_an_emptied_test_file_and_a_workflow_edit_are_flagged(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)

    def edit(r: Path) -> None:
        (r / "tests" / "test_b.py").write_text("")
        (r / ".github" / "workflows").mkdir(parents=True)
        (r / ".github" / "workflows" / "ci.yml").write_text("on: push\n")

    _assert_tampered(
        _run(repo, config, _patch(repo, "empty", edit)),
        "test_file_emptied:tests/test_b.py",
        "test_config_changed:.github/workflows/ci.yml",
    )


def test_a_failing_baseline_is_still_counted_and_cached(
    repo: Path, config: SupervisorConfig
) -> None:
    """pytest exits 1 on the baseline: its counts are still the baseline."""
    _seed(repo, failing=True)
    patch = _patch(repo, "readme", lambda r: (r / "README.md").write_text("hi\n"))
    outcome, _record, approved, _ = _run(repo, config, patch)
    assert outcome.outcome == "failed"  # the suite fails, nothing was tampered
    assert outcome.tamper_flags == ()
    assert approved is None
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    store = RunStore(config.db_path)
    try:
        cached = store.cached_test_counts(str(repo), base, PIN)
    finally:
        store.close()
    assert cached is not None
    assert cached["failed"] == 1 and cached["passed"] == 3


def test_a_baseline_cache_hit_skips_the_second_baseline_run(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(repo)
    runs: list[str] = []
    real: Callable[..., tuple[int, str, str]] = reverify_mod._run_command

    def counting(command: str, **kwargs: Any) -> tuple[int, str, str]:
        runs.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(reverify_mod, "_run_command", counting)
    first = _run(repo, config, _patch(repo, "one", lambda r: (r / "a.txt").write_text("a\n")))
    assert first[0].test_counts is not None
    assert first[0].test_counts["baseline_source"] == "run"
    assert len(runs) == 2  # baseline + patched
    second = _run(repo, config, _patch(repo, "two", lambda r: (r / "b.txt").write_text("b\n")))
    assert second[0].test_counts is not None
    assert second[0].test_counts["baseline_source"] == "cache"
    assert len(runs) == 3  # patched only
    assert second[0].outcome == "passed" and second[2] is not None


def test_unparseable_pytest_counts_block_auto_landing(repo: Path, config: SupervisorConfig) -> None:
    """No terminal reporter, no summary line: a pytest pin whose counts
    cannot be read fails closed to a human."""
    _seed(repo)
    pin = f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -p no:terminal"
    patch = _patch(repo, "quiet", lambda r: (r / "a.txt").write_text("a\n"))
    outcome, record, approved, blocked = _run(repo, config, patch, pin)
    assert outcome.outcome == "passed", outcome.detail
    assert record.test_counts["status"] == "unparseable"
    assert approved is None
    assert "test counts could not be read" in blocked[VERIFIED_PATCH_RULE.name]


def test_a_non_pytest_pin_records_unknown_counts_and_still_lands(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    (repo / "check.py").write_text("print('ok')\n")
    git(repo, "add", "check.py")
    git(repo, "commit", "-qm", "check")
    pin = f"{shlex.quote(sys.executable)} check.py"
    patch = _patch(repo, "plain", lambda r: (r / "a.txt").write_text("a\n"))
    outcome, record, approved, blocked = _run(repo, config, patch, pin)
    assert outcome.outcome == "passed", outcome.detail
    assert record.test_counts == {"status": "unknown", "runner": None}
    assert approved is not None, blocked


# --- persistence --------------------------------------------------------------


def test_the_migration_adds_v1_columns_to_an_old_db_idempotently(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE reverifications (task_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,"
            " worker_outcome TEXT, confirmed INTEGER NOT NULL, commands_json TEXT NOT NULL,"
            " exit_codes_json TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO reverifications VALUES"
            " ('t1', 'passed', 'passed', 1, '[\"true\"]', '[0]', 'old', '2026-01-01')"
        )
    for _ in range(2):  # a second open must not re-add anything
        store = RunStore(db)
        try:
            old = store.reverification_for("t1")
        finally:
            store.close()
        assert old is not None
        assert old.tamper_flags == () and old.test_counts is None
        assert old.confirmed is True
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(reverifications)")]
    assert columns.count("tamper_flags_json") == 1
    assert columns.count("test_counts_json") == 1


def test_the_run_detail_api_exposes_tamper_flags_and_counts(config: SupervisorConfig) -> None:
    client = serve_client(config)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=config.home / "ws", instructions="v1")
        store.create_run(task, repo=config.home, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed", None)
        counts = {"status": "ok", "runner": "pytest", "baseline": None, "patched": None}
        store.record_reverification(
            task.task_id,
            outcome="tampered",
            worker_outcome="passed",
            confirmed=False,
            commands=[PIN],
            exit_codes=[0],
            detail="V1",
            tamper_flags=("test_total_dropped",),
            test_counts=counts,
        )
    finally:
        store.close()
    body = client.get(f"/api/runs/{task.task_id}").json()["reverification"]
    assert body["tamper_flags"] == ["test_total_dropped"]
    assert body["test_counts"] == counts
    assert body["outcome"] == "tampered" and body["confirmed"] is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "..F\n==== 1 failed, 2 passed, 1 skipped, 3 deselected in 0.12s ====",
            {"passed": 2, "failed": 1, "skipped": 1, "deselected": 3, "total_executed": 3},
        ),
        (
            "1 passed, 1 error, 1 xfailed, 1 xpassed, 2 warnings in 1.00s (0:00:01)\nstderr",
            {"passed": 1, "errors": 1, "xfailed": 1, "xpassed": 1, "total_executed": 4},
        ),
        ("\x1b[32m3 passed\x1b[0m\x1b[32m in 0.01s\x1b[0m", {"passed": 3, "total_executed": 3}),
        ("=== no tests ran in 0.01s ===", {"total_executed": 0}),
        ("collected 3 items\nKeyboardInterrupt", None),
        # Review R1 (F9): a line with no pytest word is not a summary.
        ("2 files in 0.01s", None),
        ("3 passed in 0.01s\n2 files in 0.01s", {"passed": 3, "total_executed": 3}),
        # Review R1 (F2): two summaries (a second pytest, a printed fake) fail closed.
        ("1 passed in 0.01s\n== 2 passed in 0.02s ==", None),
    ],
)
def test_pytest_counts_reads_the_final_summary_line(
    output: str, expected: dict[str, int] | None
) -> None:
    counts = pytest_counts(output)
    if expected is None:
        assert counts is None
        return
    assert counts is not None
    for key, value in expected.items():
        assert counts[key] == value, (key, counts)


def test_an_unreadable_patch_is_a_failed_rerun_not_tampering(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    patch = repo.parent / "garbage.patch"
    patch.write_text("this is not a diff\n", encoding="utf-8")
    outcome, record, approved, _ = _run(repo, config, patch)
    assert outcome.outcome == "failed", outcome.detail
    assert record.tamper_flags == ()
    assert approved is None


# --- review round 1 -----------------------------------------------------------


def test_what_the_baseline_run_leaves_behind_never_reaches_the_patched_run(
    repo: Path, config: SupervisorConfig
) -> None:
    """F1 (I2): the cold-cache baseline runs in the worktree the patch then
    lands in. An ignored file or a temp file it wrote must not make the
    patched tests pass."""
    (repo / ".gitignore").write_text("state.txt\n")
    writer = (
        "import tempfile\nfrom pathlib import Path\n\n\n"
        "def test_state():\n"
        "    Path('state.txt').write_text('x')\n"
        "    (Path(tempfile.gettempdir()) / 'skep-v1-state').write_text('x')\n"
    )
    reader = writer.replace(
        "    Path('state.txt').write_text('x')\n"
        "    (Path(tempfile.gettempdir()) / 'skep-v1-state').write_text('x')\n",
        "    assert Path('state.txt').exists() or "
        "(Path(tempfile.gettempdir()) / 'skep-v1-state').exists()\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_state.py").write_text(writer)
    _seed_more = TEST_B  # keep the tree shape of _seed
    (repo / "tests" / "test_b.py").write_text(_seed_more)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "state")
    patch = _patch(repo, "leak", lambda r: (r / "tests" / "test_state.py").write_text(reader))
    outcome, record, approved, _ = _run(repo, config, patch)
    assert outcome.outcome == "failed", outcome.detail
    assert outcome.exit_codes == [1]
    assert record.confirmed is False and approved is None


def test_a_printed_second_summary_fails_closed(repo: Path, config: SupervisorConfig) -> None:
    """F2: a fake summary printed after pytest's own (an atexit hook) can't
    stand in for the real counts — two summaries are unreadable."""
    _seed(repo)
    fake = "import atexit\n\natexit.register(print, '==== 9 passed in 0.01s ====')\n\n\n"
    patch = _patch(repo, "fake", lambda r: (r / "tests" / "test_b.py").write_text(fake + TEST_B))
    outcome, record, approved, blocked = _run(repo, config, patch)
    assert record.test_counts["status"] == "unparseable", outcome.detail
    assert approved is None
    assert "test counts could not be read" in blocked[VERIFIED_PATCH_RULE.name]


def test_a_summary_on_stderr_is_not_counted(repo: Path, config: SupervisorConfig) -> None:
    """F2: counts come from the pin's stdout, where pytest writes its summary."""
    _seed(repo)
    fake = (
        "import atexit\nimport sys\n\n"
        "atexit.register(lambda: print('== 1 passed in 0.01s ==', file=sys.stderr))\n\n\n"
    )
    patch = _patch(repo, "stderr", lambda r: (r / "tests" / "test_b.py").write_text(fake + TEST_B))
    outcome, record, approved, blocked = _run(repo, config, patch)
    assert outcome.outcome == "passed", outcome.detail
    assert record.test_counts["patched"]["total_executed"] == 3
    assert approved is not None, blocked


def test_a_pin_that_runs_pytest_twice_fails_closed(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    pin = f"{PIN} tests/test_a.py && {PIN} tests/test_b.py"
    patch = _patch(repo, "twice", lambda r: (r / "a.txt").write_text("a\n"))
    outcome, record, approved, _ = _run(repo, config, patch, pin)
    assert outcome.outcome == "passed", outcome.detail
    assert record.test_counts["status"] == "unparseable"
    assert approved is None


def test_a_maxfail_pin_keeps_an_honest_regression_failed(
    repo: Path, config: SupervisorConfig
) -> None:
    """F3: `-x` stops at the first failure, so a real regression executes
    fewer tests. That is a failed re-run, not tampering."""
    _seed(repo)
    patch = _patch(
        repo,
        "regress",
        lambda r: (r / "tests" / "test_a.py").write_text(TEST_A.replace("1 == 1", "1 == 2")),
    )
    outcome, record, approved, _ = _run(repo, config, patch, f"{PIN} -x")
    assert outcome.outcome == "failed", outcome.detail
    assert outcome.tamper_flags == ()
    counts = record.test_counts
    assert counts["status"] == "ok"
    assert counts["patched"]["total_executed"] < counts["baseline"]["total_executed"]
    assert record.confirmed is False and approved is None


def test_a_quoted_pytest_pin_is_still_counted(repo: Path, config: SupervisorConfig) -> None:
    """F4: `-m 'pytest'` is a pytest pin, so a quietly removed failing test
    is caught by the counts."""
    _seed(repo, failing=True)
    pin = f"{shlex.quote(sys.executable)} -m 'pytest' -q -p no:cacheprovider"
    patch = _patch(repo, "quoted", lambda r: (r / "tests" / "test_a.py").write_text(TEST_A))
    _assert_tampered(_run(repo, config, patch, pin), "test_total_dropped")


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        ("pytest;", True),
        ("'pytest' -q", True),
        ('"python" -m pytest', True),
        ("python -m pytest", True),
        ("uv run pytest -x", True),
        ("cd sub && CI=1 py.test", True),
        ("uv run --with pytest-xdist pytest -n auto", True),
        ("sh -c 'pytest -q'", True),
        ("/usr/bin/pytest", True),
        ("grep pytest x", False),
        ("echo pytest-ish", False),
        ("npm test", False),
        ("uv run --with pytest-xdist python check.py", False),
        ("pytest 'unclosed", True),  # unreadable: fail closed
    ],
)
def test_pytest_pins_are_read_from_shell_words(pin: str, expected: bool) -> None:
    assert reverify_mod._is_pytest_pin(pin) is expected


def test_a_deleted_test_file_with_a_quoted_path_is_flagged(
    repo: Path, config: SupervisorConfig
) -> None:
    """F5: git quotes non-ASCII paths; the guard reads NUL-delimited names."""
    _seed(repo)
    odd = repo / "tests" / "test_é x.py"
    odd.write_text(TEST_B.replace("three", "four"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "odd")
    patch = _patch(repo, "odd", lambda r: (r / "tests" / "test_é x.py").unlink())
    _assert_tampered(_run(repo, config, patch), "test_file_deleted:tests/test_é x.py")


def test_a_modified_binary_fixture_under_tests_does_not_crash(
    repo: Path, config: SupervisorConfig
) -> None:
    """F5: a binary blob is read as bytes, never decoded as text."""
    _seed(repo)
    (repo / "tests" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01" * 64)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "blob")
    patch = _patch(
        repo, "blob", lambda r: (r / "tests" / "blob.bin").write_bytes(b"\xfe\xff\x00\x02" * 64)
    )
    outcome, record, approved, blocked = _run(repo, config, patch)
    assert outcome.outcome == "passed", outcome.detail
    assert record.tamper_flags == ()
    assert approved is not None, blocked


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        ("vite.config.ts", None, "export default {}\n"),
        ("setup.py", "VERSION = '1.0'\n", "VERSION = '1.1'\n"),
        (
            "setup.cfg",
            "[metadata]\nversion = 1.0\n\n[tool:pytest]\naddopts = -q\n",
            "[metadata]\nversion = 1.1\n\n[tool:pytest]\naddopts = -q\n",
        ),
    ],
)
def test_build_config_edits_are_not_test_tampering(
    repo: Path, config: SupervisorConfig, name: str, before: str | None, after: str
) -> None:
    """F7 (I8): a file that steers the check but is not test config keeps its
    `confirmed` verdict; the landing gate still sends it to a human."""
    _seed(repo)
    if before is not None:
        (repo / name).write_text(before)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "config")
    outcome, record, approved, blocked = _run(
        repo, config, _patch(repo, "cfg", lambda r: (r / name).write_text(after))
    )
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.tamper_flags == ()
    assert record.confirmed is True
    assert approved is None
    assert "decide what the pinned check runs" in blocked[VERIFIED_PATCH_RULE.name]


def test_a_setup_cfg_pytest_section_edit_is_tampering(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    (repo / "setup.cfg").write_text("[tool:pytest]\naddopts = -q\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cfg")
    patch = _patch(
        repo,
        "deselect",
        lambda r: (r / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = -q --deselect tests/test_b.py::test_three\n"
        ),
    )
    _assert_tampered(_run(repo, config, patch), "test_config_changed:setup.cfg")


def test_an_xfail_call_over_a_failing_test_is_tampered(
    repo: Path, config: SupervisorConfig
) -> None:
    """F8: `pytest.xfail(...)` turns a failing test into an xfail that still
    counts as executed — the marker and the xfailed count both say so."""
    _seed(repo, failing=True)
    patch = _patch(
        repo,
        "xfail",
        lambda r: (r / "tests" / "test_a.py").write_text(
            "import pytest\n\n\n"
            + TEST_A
            + FAILING.replace("assert 1 == 2", "pytest.xfail('later')")
        ),
    )
    _assert_tampered(
        _run(repo, config, patch), "skip_marker_added:tests/test_a.py", "test_xfailed_increased"
    )


def test_an_xfail_call_plus_a_noop_test_is_tampered(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    patch = _patch(
        repo,
        "xfail-noop",
        lambda r: (r / "tests" / "test_a.py").write_text(
            "import pytest\n\n\n"
            + TEST_A.replace("assert 2 == 2", "pytest.xfail('later')")
            + "\n\ndef test_noop():\n    pass\n"
        ),
    )
    _assert_tampered(_run(repo, config, patch), "test_xfailed_increased")


def test_a_guard_that_cannot_read_the_patch_fails_closed(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: nothing the guard's git calls raise escapes before the verdict is
    persisted — it is a flag, so the run is not confirmed."""
    _seed(repo)
    patch = _patch(repo, "any", lambda r: (r / "a.txt").write_text("a\n"))

    def broken(*_args: object) -> tuple[str, ...]:
        raise OSError("git vanished")

    monkeypatch.setattr(reverify_mod, "patch_paths", broken)
    flags = reverify_mod.static_tamper_flags(repo, patch, "HEAD")
    assert flags == ["tamper_check_failed:OSError"]
