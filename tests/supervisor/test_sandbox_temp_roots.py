"""ES-F5 (B11 + B46, I12): no shared temp roots, and no skep home inside a wall.

The Seatbelt profile made /tmp, /private/tmp, /var/folders (and their
resolved twins) writable for every run: run B read run A's files, and a
SKEP_HOME under $TMPDIR — the lane-isolation trap in plan §3.0 — let a
sandboxed worker rewrite the store, baseline and token (mac-p1 P1-d).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import sandbox


@pytest.fixture(autouse=True)
def seatbelt_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend="seatbelt"),
    )
    yield


def test_seatbelt_profile_has_no_shared_temp_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    text = sandbox.write_profile(
        tmp_path / "p.sb", workspace=workspace, skep_home=tmp_path / "home"
    ).read_text(encoding="utf-8")
    writes = next(
        line for line in text.splitlines() if line.startswith("(allow file-write* (subpath")
    )
    assert writes == f'(allow file-write* (subpath "{workspace}"))'
    for shared in ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders"):
        assert f'(subpath "{shared}")' not in text


@pytest.mark.parametrize("where", ["workspace", "extra"])
def test_a_skep_home_inside_a_writable_root_refuses_the_run(tmp_path: Path, where: str) -> None:
    root = tmp_path / "root"
    home = root / "nested" / "skep"
    home.mkdir(parents=True)
    workspace, extra = (root, []) if where == "workspace" else (tmp_path / "ws", [root])
    with pytest.raises(sandbox.SandboxUnavailableError, match="lies inside the worker-writable"):
        sandbox.write_profile(
            tmp_path / "p.sb", workspace=workspace, extra_writable=extra, skep_home=home
        )


def test_the_home_is_the_callers_never_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mac finding A: `skep serve --home X` does not export SKEP_HOME, so an
    env fallback guarded the wrong home. A decoy $SKEP_HOME changes nothing."""
    monkeypatch.setenv("SKEP_HOME", str(tmp_path / "ws" / "home"))
    sandbox.write_profile(
        tmp_path / "p.sb", workspace=tmp_path / "ws", skep_home=tmp_path / "skep-home"
    )
