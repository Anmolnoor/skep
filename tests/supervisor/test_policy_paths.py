"""B1-2 (ADR 0054 A8): ``workspace_value`` is symlink-aware and fails closed."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor.policy_paths import workspace_value


def test_a_symlink_out_of_the_workspace_is_literal(tmp_path: Path) -> None:
    cwd, outside = tmp_path / "repo", tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    (cwd / "link").symlink_to(outside)
    path = f"{cwd}/link/new.txt"
    assert workspace_value(path, str(cwd)) == path


def test_dotdot_escape_is_literal(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    path = f"{tmp_path}/repo/../escape.txt"
    assert workspace_value(path, str(tmp_path / "repo")) == path


def test_new_and_relative_files_inside_are_workspace(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    assert workspace_value(f"{cwd}/not/yet/there.txt", cwd) == "workspace"
    assert workspace_value("sub/new.txt", cwd) == "workspace"


def test_a_symlinked_cwd_contains_its_real_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    assert workspace_value(f"{real}/a.py", str(tmp_path / "alias")) == "workspace"


def test_a_cwd_that_is_not_a_directory_contains_nothing(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    assert workspace_value(f"{gone}/a.py", str(gone)) == f"{gone}/a.py"


def test_empty_arguments_keep_their_old_meaning() -> None:
    assert workspace_value("", "/x") == "workspace"
    assert workspace_value("/etc/hosts", "") == "/etc/hosts"
