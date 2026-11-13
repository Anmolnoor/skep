"""W32 (ledger B44): a filesystem rule binds whichever spelling of a path it
was written in. On macOS ``/tmp`` is ``/private/tmp``; the Queen's file lane
decides the resolved path, REST / the CLI / the hook the path as typed. The
matcher judges both spellings of the path against both spellings of the
rule's literal prefix, so every face gives one answer. A symlinked directory
under ``tmp_path`` stands in for ``/tmp`` on Linux; a macOS-only test uses
the real ``/tmp``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
)
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.fileio import queen_filesystem_decision
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import predating_home


@pytest.fixture(autouse=True)
def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    predating_home(config.db_path)
    store = RunStore(config.db_path)
    yield store
    store.close()


@pytest.fixture()
def holder(config: SupervisorConfig, store: RunStore) -> ConfigHolder:
    return ConfigHolder(config, store)


def _rules(store: RunStore, *, deny: Sequence[str] = (), allow: Sequence[str] = ()) -> None:
    def rules(patterns: Sequence[str], kind: str) -> list[PolicyRule]:
        return [
            PolicyRule(rule_id=f"{kind}-{action}-{i}", action=action, pattern=pattern)
            for i, pattern in enumerate(patterns)
            for action in ("read", "write")
        ]

    document = PolicyDocument(
        scopes=[ScopePolicy(scope="filesystem", deny=rules(deny, "no"), allow=rules(allow, "ok"))]
    )
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())


def _every_face(store: RunStore, holder: ConfigHolder, action: str, path: str) -> set[str]:
    """read_file's lane (resolves first) and REST / the CLI (as typed)."""
    lane = queen_filesystem_decision(store, holder, action=action, path=path)
    typed = policy_decide(holder, store, scope="filesystem", action=action, value=path)
    return {lane.verdict, typed["verdict"]}


@pytest.fixture()
def spellings(tmp_path: Path) -> tuple[Path, Path]:
    """``link`` -> ``real``: the /tmp -> /private/tmp shape, on any OS."""
    real = tmp_path / "private-tmp"
    (real / "canary-dir").mkdir(parents=True)
    (real / "canary-dir" / "a.txt").write_text("CANARY-w32\n", encoding="utf-8")
    link = tmp_path / "tmp"
    link.symlink_to(real, target_is_directory=True)
    return link, real


@pytest.mark.parametrize("rule_side", ["link", "real"])
def test_a_deny_binds_either_spelling_of_the_path_on_every_face(
    store: RunStore, holder: ConfigHolder, spellings: tuple[Path, Path], rule_side: str
) -> None:
    link, real = spellings
    base = link if rule_side == "link" else real
    _rules(store, deny=[f"{base}/canary-dir/**"])
    for root in (link, real):
        for action in ("read", "write"):
            assert _every_face(store, holder, action, f"{root}/canary-dir/a.txt") == {"deny"}, (
                root,
                action,
            )


def test_deny_beats_allow_across_the_two_spellings(
    store: RunStore, holder: ConfigHolder, spellings: tuple[Path, Path]
) -> None:
    """The allow is written in the longer spelling: it must not out-rank a
    deny on the same directory just by being spelled with more characters."""
    link, real = spellings
    _rules(store, deny=[f"{link}/canary-dir/**"], allow=[f"{real}/canary-dir/**"])
    for root in (link, real):
        assert _every_face(store, holder, "read", f"{root}/canary-dir/a.txt") == {"deny"}, root
    _rules(store, deny=[f"{real}/canary-dir/**"], allow=[f"{link}/canary-dir/**"])
    for root in (link, real):
        assert _every_face(store, holder, "read", f"{root}/canary-dir/a.txt") == {"deny"}, root


def test_an_operator_root_binds_the_path_typed_through_a_link(
    store: RunStore, holder: ConfigHolder, config: SupervisorConfig, tmp_path: Path
) -> None:
    """Roots are stored resolved; a path typed through a link into the skep
    home is still inside it on REST / the CLI, as it is on read_file."""
    link = tmp_path / "home-link"
    link.symlink_to(config.home.resolve(), target_is_directory=True)
    path = f"{link}/notes.txt"
    lane = queen_filesystem_decision(store, holder, action="read", path=path)
    typed = policy_decide(holder, store, scope="filesystem", action="read", value=path)
    assert (lane.verdict, typed["verdict"]) == ("allow", "allow")
    assert typed["decided_by"] == "global/operator-root"


def test_a_path_with_no_symlink_keeps_its_verdict(
    store: RunStore, holder: ConfigHolder, spellings: tuple[Path, Path], tmp_path: Path
) -> None:
    _, real = spellings
    _rules(store, deny=[f"{real}/canary-dir/**"])
    other = tmp_path / "elsewhere" / "b.txt"
    typed = policy_decide(holder, store, scope="filesystem", action="read", value=str(other))
    assert typed["verdict"] == "require_approval"


@pytest.mark.skipif(sys.platform != "darwin", reason="/tmp is /private/tmp only on macOS")
def test_the_real_tmp_and_private_tmp_are_one_directory_to_a_rule(
    store: RunStore, holder: ConfigHolder
) -> None:
    made = Path(tempfile.mkdtemp(prefix="skep-w32-", dir="/tmp"))
    try:
        name = made.name
        (Path("/private/tmp") / name / "a.txt").write_text("CANARY-w32\n", encoding="utf-8")
        for rule_root in ("/tmp", "/private/tmp"):
            _rules(store, deny=[f"{rule_root}/{name}/**"])
            for path_root in ("/tmp", "/private/tmp"):
                for action in ("read", "write"):
                    got = _every_face(store, holder, action, f"{path_root}/{name}/a.txt")
                    assert got == {"deny"}, (rule_root, path_root, action)
    finally:
        shutil.rmtree(made, ignore_errors=True)
