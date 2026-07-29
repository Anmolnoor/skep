"""v51-F2: Queen file reads — read_file / search_files behind the filesystem scope.

The invariant: paths inside the operator roots (skep home, repos root,
workon-bound project paths) read inside the turn; any other path cards; an
explicit filesystem deny rule refuses without a card — and resolution
happens on the REAL path, so symlinks cannot smuggle a read out of a root.
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
)
from skep.supervisor.serve.fileio import (
    operator_roots,
    queen_filesystem_decision,
    read_file_result,
    search_files_result,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    store = RunStore(config.db_path)
    yield store
    store.close()


@pytest.fixture()
def holder(config: SupervisorConfig, store: RunStore) -> ConfigHolder:
    return ConfigHolder(config, store)


# ---------- the decision ----------


def test_reads_are_mutating_tier_for_gating() -> None:
    """The card machinery lives in the mutating tier (the call_mcp_tool
    precedent) — that is what lets an out-of-root path pause into a card."""
    assert "read_file" in MUTATING_TOOL_NAMES
    assert "search_files" in MUTATING_TOOL_NAMES


def test_home_allows_and_elsewhere_cards(
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    inside = queen_filesystem_decision(
        store, holder, action="read", path=str(config.home / "notes.txt")
    )
    assert inside.verdict == "allow"
    assert inside.reason == "filesystem.allow.operator_root"
    # v52-F3: decisions resolve through the composed operator policy; with no
    # global template the label is the operator default's.
    assert inside.decided_by == "operator-default/operator-root"

    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("real file")  # v59-F9: only an EXISTING path cards
    outside = queen_filesystem_decision(store, holder, action="read", path=str(elsewhere))
    assert outside.verdict == "require_approval"
    assert outside.reason == "filesystem.require_approval.outside_operator_roots"
    assert outside.decided_by == "operator-default/outside-operator-roots"


def test_nonexistent_outside_path_fails_fast_without_a_card(
    store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    """v59-F9: a probe of an invented path is denied immediately — the card
    protects reading a real file; ~15 hallucinated-path cards interrupted the
    operator in the 2026-07-18 field test while protecting nothing."""
    decision = queen_filesystem_decision(
        store, holder, action="read", path=str(tmp_path / "SKEP_HOME" / "repos" / "docs")
    )
    assert decision.verdict == "deny"
    assert decision.reason == "filesystem.deny.no_such_path"
    assert decision.decided_by == "operator-default/no-such-path"
    assert decision.detail == str(tmp_path / "SKEP_HOME" / "repos" / "docs")


def test_missing_path_is_denied(store: RunStore, holder: ConfigHolder) -> None:
    decision = queen_filesystem_decision(store, holder, action="read", path="  ")
    assert decision.verdict == "deny"
    assert decision.reason == "filesystem.deny.missing_path"


def test_symlink_out_of_a_root_still_cards(
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    """The decision sees the RESOLVED path — a symlink inside the home
    pointing outside is judged by where it lands."""
    secret = tmp_path / "secret.txt"
    secret.write_text("shh")
    link = config.home / "innocent.txt"
    link.symlink_to(secret)
    decision = queen_filesystem_decision(store, holder, action="read", path=str(link))
    assert decision.verdict == "require_approval"
    assert decision.detail == str(secret)


def test_explicit_filesystem_rules_beat_the_root_fallback(
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    """A deny rule refuses INSIDE an operator root; an allow rule frees a
    path outside every root. Both decisions name their rule."""
    document = PolicyDocument(
        scopes=[
            ScopePolicy(
                scope="filesystem",
                deny=[
                    PolicyRule(
                        rule_id="no-vault",
                        action="read",
                        pattern=f"{config.home}/vault/*",
                    )
                ],
                allow=[
                    PolicyRule(
                        rule_id="datasets",
                        action="read",
                        pattern=f"{tmp_path}/datasets/*",
                    )
                ],
            )
        ]
    )
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())

    denied = queen_filesystem_decision(
        store, holder, action="read", path=str(config.home / "vault" / "token")
    )
    assert denied.verdict == "deny"
    assert denied.decided_by == "operator-default/no-vault"

    allowed = queen_filesystem_decision(
        store, holder, action="read", path=str(tmp_path / "datasets" / "a.csv")
    )
    assert allowed.verdict == "allow"
    assert allowed.decided_by == "operator-default/datasets"


def test_operator_document_rules_now_govern_queen_reads(
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    """v52-F3: a rule in the OPERATOR document (Queen-only, never read by
    workers) frees a path outside every root — the capability the composed
    resolution adds over v51-F2."""
    from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY

    document = PolicyDocument(
        template="operator-default",
        scopes=[
            ScopePolicy(
                scope="filesystem",
                allow=[
                    PolicyRule(
                        rule_id="op:scratch",
                        action="read",
                        pattern=f"{tmp_path}/scratch/*",
                    )
                ],
            )
        ],
    )
    store.set_setting(OPERATOR_POLICY_SETTINGS_KEY, document.model_dump_json())

    decision = queen_filesystem_decision(
        store, holder, action="read", path=str(tmp_path / "scratch" / "notes.md")
    )
    assert decision.verdict == "allow"
    assert decision.decided_by == "operator-default/op:scratch"


def test_workon_bound_project_path_is_an_operator_root(
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder, tmp_path: Path
) -> None:
    workdir = tmp_path / "my-app"
    workdir.mkdir()
    store.add_project_policy(
        project_id="my-app",
        name="my-app",
        strategy="trusted_local_dev",
        phase="build",
        policy={},
    )
    store.add_project_binding(
        project_id="my-app", binding_kind="repo_path", binding_value=str(workdir)
    )
    assert workdir.resolve() in operator_roots(store, holder)
    decision = queen_filesystem_decision(
        store, holder, action="read", path=str(workdir / "README.md")
    )
    assert decision.verdict == "allow"


# ---------- the executors ----------


def test_read_file_returns_numbered_bounded_lines(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n" + "x" * 900 + "\n")
    result = read_file_result(str(target))
    assert result["total_lines"] == 4
    assert result["content"].splitlines()[0] == "1\talpha"
    # long lines are truncated, marked, and never dropped
    assert result["content"].splitlines()[3].endswith(" …")

    window = read_file_result(str(target), offset=2, limit=2)
    assert window["lines_shown"] == 2
    assert window["content"].splitlines() == ["2\tbeta", "3\tgamma"]

    missing = read_file_result(str(tmp_path / "nope.txt"))
    assert "not a file" in missing["error"]


def test_search_files_content_and_names(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def needle():\n    pass\n")
    (tmp_path / "b.txt").write_text("no matches here\n")
    content = search_files_result("needle", path=str(tmp_path))
    assert len(content["matches"]) == 1
    assert "a.py" in content["matches"][0]
    assert ":1:" in content["matches"][0]

    names = search_files_result("*.py", path=str(tmp_path), target="files")
    assert [Path(m).name for m in names["matches"]] == ["a.py"]

    globbed = search_files_result("needle", path=str(tmp_path), file_glob="*.txt")
    assert globbed["matches"] == []

    missing = search_files_result("x", path=str(tmp_path / "ghost"))
    assert "no such path" in missing["error"]


# ---------- end to end through the chat ----------


def test_read_inside_root_executes_in_turn(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client, chat_id = chat_client(config, ollama)
    target = config.home / "hello.txt"
    target.write_text("alpha\nbeta\n")
    ollama.script_tool_call("read_file", {"path": str(target)})
    ollama.script_reply("the file says alpha")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "read it"}).text
    )
    tools = [d for name, d in events if name == "tool"]
    assert tools[0]["tool"] == "read_file"
    assert tools[0]["result"]["ok"] is True
    assert "1\talpha" in tools[0]["result"]["result"]["content"]
    # v52-F5: the admitting rule rides the result — the transcript is the audit.
    assert tools[0]["result"]["result"]["decided_by"] == "operator-default/operator-root"
    assert tools[0]["decision"]["reason"] == "filesystem.allow.operator_root"
    assert [d for name, d in events if name == "action"] == []  # no card
    assert events[-1] == ("done", {"state": "complete"})


def test_read_outside_roots_cards_then_confirm_reads(
    config: SupervisorConfig, ollama: FakeOllama, tmp_path: Path
) -> None:
    client, chat_id = chat_client(config, ollama)
    target = tmp_path / "elsewhere.txt"
    target.write_text("outside content\n")
    ollama.script_tool_call("read_file", {"path": str(target)})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "read it"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "read_file"
    # The card names the exact resolved path the operator would be granting.
    assert actions[0]["decision"]["detail"] == str(target.resolve())
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("here is the file")
    confirm = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert confirm[-1] == ("done", {"state": "complete"})
    resolved = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert resolved["status"] == "confirmed"
    assert "outside content" in resolved["result"]["result"]["content"]


def test_deny_rule_refuses_without_a_card(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        document = PolicyDocument(
            scopes=[
                ScopePolicy(
                    scope="filesystem",
                    deny=[
                        PolicyRule(
                            rule_id="no-vault",
                            action="read",
                            pattern=f"{config.home}/vault/*",
                        )
                    ],
                )
            ]
        )
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    finally:
        store.close()
    vault = config.home / "vault"
    vault.mkdir()
    (vault / "key.txt").write_text("secret")

    ollama.script_tool_call("read_file", {"path": str(vault / "key.txt")})
    ollama.script_reply("that path is denied")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "read the key"}).text
    )
    tools = [d for name, d in events if name == "tool"]
    assert tools[0]["result"]["ok"] is False
    assert "denied by policy" in tools[0]["result"]["error"]
    assert [d for name, d in events if name == "action"] == []  # deny never cards
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []


# ---------- v79-F3: branch-aware reads ----------


def _repo_with_landed_branch(tmp_path: Path) -> Path:
    from .conftest import git

    repo = tmp_path / "landed-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "seed")
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "checkout", "-qb", "skep/maintain")
    (repo / "landed.py").write_text("x = 1\ny = 2\nz = 3\n")
    git(repo, "add", "landed.py")
    git(repo, "commit", "-qm", "landed work")
    git(repo, "checkout", "-q", default)
    return repo


def test_read_file_explicit_ref_reads_landed_content(store: RunStore, tmp_path: Path) -> None:
    """v79-F3: an explicit ref reads via git show — the checkout never moves."""
    from skep.supervisor.serve.fileio import read_file_branch_aware

    repo = _repo_with_landed_branch(tmp_path)
    result = read_file_branch_aware(store, str(repo / "landed.py"), ref="skep/maintain")
    assert result["ref"] == "skep/maintain"
    assert "1\tx = 1" in result["content"]

    limited = read_file_branch_aware(
        store, str(repo / "landed.py"), ref="skep/maintain", offset=2, limit=1
    )
    assert limited["content"] == "2\ty = 2"
    assert limited["total_lines"] == 3


def test_read_file_falls_back_to_the_projects_landing_branch(
    store: RunStore, tmp_path: Path
) -> None:
    """v79-F3: no ref + missing on disk + a bound landing branch = the read
    the Queen meant; the result says where the content came from (I8)."""
    from skep.supervisor.serve.fileio import read_file_branch_aware

    repo = _repo_with_landed_branch(tmp_path)
    store.add_project_policy(
        project_id="landed-project",
        name="landed project",
        strategy="trusted_local_dev",
        phase="maintain",
        policy={
            "default_execution_mode": "workspace",
            "auto_apply_verified_patch": True,
            "auto_apply_branch": "skep/maintain",
        },
    )
    store.add_project_binding(
        project_id="landed-project",
        binding_kind="repo_path",
        binding_value=str(repo),
    )

    result = read_file_branch_aware(store, str(repo / "landed.py"))
    assert result["ref"] == "skep/maintain"
    assert "landing branch" in result["note"]
    assert "1\tx = 1" in result["content"]


def test_read_file_miss_teaches_the_landing_branches(store: RunStore, tmp_path: Path) -> None:
    """v79-F3 (I9): with no binding to fall back on, the error names the
    checked-out branch and the skep/ branches instead of lying 'not a file'."""
    from skep.supervisor.serve.fileio import read_file_branch_aware

    repo = _repo_with_landed_branch(tmp_path)
    result = read_file_branch_aware(store, str(repo / "landed.py"))
    assert "skep/maintain" in result["error"]
    assert "pass ref" in result["error"]


def test_read_file_on_disk_is_untouched_by_branch_awareness(
    store: RunStore, tmp_path: Path
) -> None:
    from skep.supervisor.serve.fileio import read_file_branch_aware

    repo = _repo_with_landed_branch(tmp_path)
    result = read_file_branch_aware(store, str(repo / "README.md"))
    assert "ref" not in result
    assert result["content"] == "1\tbase"


def test_deny_rule_still_denies_a_ref_read(config: SupervisorConfig, ollama: FakeOllama) -> None:
    """v79-F3 (I5): the guard decided the PATH; a ref does not reopen it."""
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        document = PolicyDocument(
            scopes=[
                ScopePolicy(
                    scope="filesystem",
                    deny=[
                        PolicyRule(
                            rule_id="no-vault",
                            action="read",
                            pattern=f"{config.home}/vault/*",
                        )
                    ],
                )
            ]
        )
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    finally:
        store.close()
    vault = config.home / "vault"
    vault.mkdir()
    (vault / "key.txt").write_text("secret")

    ollama.script_tool_call("read_file", {"path": str(vault / "key.txt"), "ref": "skep/maintain"})
    ollama.script_reply("that path is denied")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "read the key"}).text
    )
    tools = [d for name, d in events if name == "tool"]
    assert tools[0]["result"]["ok"] is False
