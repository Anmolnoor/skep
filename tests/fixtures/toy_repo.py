"""Toy repo fixture generator: a tiny Python package with one failing test."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

BUGGY_MATH_UTILS = textwrap.dedent(
    '''\
    """Math helpers for the toy package."""


    def clamp(value: int, low: int, high: int) -> int:
        if value < low:
            return high  # BUG: values below the range must clamp to `low`
        if value > high:
            return high
        return value
    '''
)

FIXED_MATH_UTILS = textwrap.dedent(
    '''\
    """Math helpers for the toy package."""


    def clamp(value: int, low: int, high: int) -> int:
        if value < low:
            return low
        if value > high:
            return high
        return value
    '''
)

TEST_MATH = textwrap.dedent(
    """\
    from toypkg.math_utils import clamp


    def test_clamp_below() -> None:
        assert clamp(-5, 0, 10) == 0


    def test_clamp_above() -> None:
        assert clamp(15, 0, 10) == 10


    def test_clamp_inside() -> None:
        assert clamp(5, 0, 10) == 5
    """
)

CONFTEST = textwrap.dedent(
    """\
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    """
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def create_toy_repo(path: Path) -> Path:
    """Create a committed git repo whose test suite fails on the clamp bug."""
    path.mkdir(parents=True)
    (path / "toypkg").mkdir()
    (path / "tests").mkdir()
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    (path / "toypkg" / "__init__.py").write_text("")
    (path / "toypkg" / "math_utils.py").write_text(BUGGY_MATH_UTILS)
    (path / "tests" / "test_math.py").write_text(TEST_MATH)
    (path / "conftest.py").write_text(CONFTEST)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "toy@example.com")
    _git(path, "config", "user.name", "Toy")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "toy package with one deliberately failing test")
    return path


# --- audit-caste fixture (U1): a repo with a flagged dependency pin -----------

# requests 2.28.0 is below the audit worker's safe pin (2.31.0); click is fine.
AUDIT_REQUIREMENTS = "click==8.1.7\nrequests==2.28.0\n"

AUDIT_PKG = textwrap.dedent(
    '''\
    """A tiny module the audit repo's own tests cover (offline — no deps imported)."""


    def greet(name: str) -> str:
        return f"hello, {name}"
    '''
)

AUDIT_TEST = textwrap.dedent(
    """\
    from auditpkg.core import greet


    def test_greet() -> None:
        assert greet("world") == "hello, world"
    """
)

# A deliberately failing suite: the audit worker still bumps the flagged pin, but
# `pytest` fails, so the run never reaches `completed` and G10 never confirms it —
# the shape v4's test gate must reject (a candidate that fails its test).
AUDIT_TEST_FAILING = textwrap.dedent(
    """\
    from auditpkg.core import greet


    def test_greet() -> None:
        assert greet("world") == "this assertion is wrong on purpose"
    """
)


# A major-version bump (urllib3 1.x -> 2.x): the audit risk-flags it, so U1 files
# it for review instead of auto-landing it.
AUDIT_REQUIREMENTS_MAJOR_BUMP = "click==8.1.7\nurllib3==1.26.0\n"


def create_audit_toy_repo(
    path: Path, *, requirements: str | None = None, passing: bool = True
) -> Path:
    """A committed repo whose requirements pin a flagged dependency.

    The test suite passes and does not import the pinned package — so the audit
    worker can bump the manifest and confirm the project still builds, fully
    offline (real reinstall-and-test against the new version needs network; the
    Stage C allowlist supplies that for real runs). ``requirements`` overrides the
    default (safe minor bump) — pass ``AUDIT_REQUIREMENTS_MAJOR_BUMP`` for the
    risk-flagged 'file for review' case. ``passing=False`` ships a failing suite
    (v4: a candidate whose test must fail the gate)."""
    path.mkdir(parents=True)
    (path / "auditpkg").mkdir()
    (path / "tests").mkdir()
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    (path / "auditpkg" / "__init__.py").write_text("")
    (path / "auditpkg" / "core.py").write_text(AUDIT_PKG)
    (path / "tests" / "test_core.py").write_text(AUDIT_TEST if passing else AUDIT_TEST_FAILING)
    (path / "requirements.txt").write_text(requirements or AUDIT_REQUIREMENTS)
    (path / "conftest.py").write_text(CONFTEST)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "toy@example.com")
    _git(path, "config", "user.name", "Toy")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "audit toy: flagged pin, passing suite")
    return path
