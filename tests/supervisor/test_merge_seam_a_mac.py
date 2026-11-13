"""The merge of hardening A (ES-F49/F51/F52) with es-mac-hardening (ES-mac-F1,
ES-F41 follow-up): both rewrote ``policy_schema.rule_matches`` and the
standing-shell-allow refusal. One document holds a shell deny and a
filesystem deny side by side, so each case passes only while the program
spellings (A) and the path spellings (mac) bind together.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
    decide,
    resolve,
)
from skep.supervisor.serve.actions import policy_decide, queen_shell_allow_refusal
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


def _rule(kind: str, action: str, pattern: str) -> PolicyRule:
    return PolicyRule(rule_id=f"{kind}-{action}-{pattern}", action=action, pattern=pattern)


def _document(link: Path, allowed: Path) -> PolicyDocument:
    """A curl deny on ``run`` beside allows for every spelling of it, and a
    filesystem deny written through a symlinked directory beside an allow on
    a directory holding a link that points out of it."""
    return PolicyDocument(
        scopes=[
            ScopePolicy(
                scope="shell",
                deny=[_rule("no", "run", "curl")],
                allow=[_rule("ok", action, "*") for action in ("run", "run_background")],
            ),
            ScopePolicy(
                scope="filesystem",
                deny=[_rule("no", "read", f"{link}/secret/**")],
                allow=[_rule("ok", "read", f"{allowed}/**")],
            ),
        ]
    )


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``link`` -> ``real`` (the /tmp -> /private/tmp shape) and an allowed
    directory whose ``out`` entry is a link to a file outside it."""
    real = tmp_path / "real"
    (real / "secret").mkdir(parents=True)
    (real / "secret" / "a.txt").write_text("CANARY-seam\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("CANARY-outside\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "out").symlink_to(outside)
    (allowed / "in.txt").write_text("ok\n", encoding="utf-8")
    return link, real, allowed


@pytest.mark.parametrize(
    ("action", "line"),
    [("run", "/usr/bin/curl https://x.example/y"), ("run", "env curl https://x.example/y"),
     ("run", "CURL https://x.example/y"), ("run_background", "curl https://x.example/y")],
)  # fmt: skip
def test_one_composition_binds_program_spellings_and_path_spellings(
    tree: tuple[Path, Path, Path], action: str, line: str
) -> None:
    link, real, allowed = tree
    resolved = resolve(_document(link, allowed))
    assert decide(resolved, "shell", action, line).verdict == "deny", line
    assert decide(resolved, "shell", "run", "grep curl README.md").verdict == "allow"
    for root in (link, real):
        assert decide(resolved, "filesystem", "read", f"{root}/secret/a.txt").verdict == "deny"
    assert decide(resolved, "filesystem", "read", f"{allowed}/out").verdict == "deny"  # unmatched
    assert decide(resolved, "filesystem", "read", f"{allowed}/in.txt").verdict == "allow"


def test_every_face_gives_both_answers_from_one_stored_document(
    store: RunStore, holder: ConfigHolder, tree: tuple[Path, Path, Path]
) -> None:
    link, real, allowed = tree
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, _document(link, allowed).model_dump_json())

    def faces(path: str) -> set[str]:
        lane = queen_filesystem_decision(store, holder, action="read", path=path)
        typed = policy_decide(holder, store, scope="filesystem", action="read", value=path)
        return {lane.verdict, typed["verdict"]}

    for root in (link, real):
        assert faces(f"{root}/secret/a.txt") == {"deny"}, root
    # the allow is judged on the resolved path only: a link out of it asks
    assert faces(f"{allowed}/out") == {"require_approval"}
    assert faces(f"{allowed}/in.txt") == {"allow"}
    for action, line in (("run", "/usr/bin/curl https://x.example/y"),
                         ("run", "timeout 5 curl https://x.example/y"),
                         ("run_background", "curl https://x.example/y")):  # fmt: skip
        hit = policy_decide(holder, store, scope="shell", action=action, value=line)
        assert hit["verdict"] == "deny", (action, line)


@pytest.mark.parametrize(
    "line",
    [
        "nice curl https://x.example/y",  # mac: the program that finally runs
        "python3.12 x.py",  # mac: a versioned interpreter
        "docker run x",  # mac: a remote-acting tool
        "npm exec x",  # mac: a verb that fetches and runs, now a shared install verb
        "pip3.12 install x",  # mac: a versioned package manager
        "yarn",  # mac: a bare package manager
        "dig example.com",  # A: the shared network list
        "ping x",  # A: the shared network list
    ],
)
def test_the_standing_shell_allow_refuses_what_either_side_refused(line: str) -> None:
    assert isinstance(queen_shell_allow_refusal(line, "run"), str), line


def test_a_harmless_standing_shell_allow_still_stores() -> None:
    assert queen_shell_allow_refusal("ls -la", "run") == [("shell", "run", "ls -la", "exact_argv")]
