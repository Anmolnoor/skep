"""v25-F2: /workon — local directories become first-class, THROUGH git.

A non-git dir gets a confirmed git baseline (init + commit) before binding;
an existing repo is bound without touching its tree; the store is never a
workspace. Every skep guarantee is a git guarantee, so there is no raw-
filesystem mode — that is a different, worse product.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.tools import COMMAND_TOOL_NAMES, MUTATING_TOOL_NAMES

from .conftest import git, serve_client


def test_workon_inits_commits_baseline_and_binds_a_plain_dir(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    workdir = tmp_path / "plain-Project"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text("[project]\nname = 'plain'\n")
    (workdir / "main.py").write_text("print('hi')\n")

    client = serve_client(config)
    preview = client.post("/api/workon/preview", json={"path": str(workdir)})
    assert preview.status_code == 200
    view = preview.json()
    assert view["would_git_init"] is True
    assert view["would_commit_baseline"] is True
    assert view["project_id"] == "plain-project"
    # Previewing changed nothing.
    assert not (workdir / ".git").exists()

    done = client.post("/api/workon", json={"path": str(workdir)})
    assert done.status_code == 201
    result = done.json()
    assert result["git_initialized"] is True
    assert result["baseline_committed"] is True
    assert (workdir / ".git").is_dir()
    log = git(workdir, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1  # the baseline commit, nothing else
    assert git(workdir, "status", "--porcelain").stdout.strip() == ""

    # Binding is what grants trust: the effective policy shows it.
    policy = result["effective_policy"]
    assert policy["project"]["project_id"] == "plain-project"
    assert policy["execution_mode"] == "workspace"
    assert policy["trust_root"] is not None
    # Toolchain seeding ran (pyproject.toml -> pytest commands).
    assert ["uv", "run", "pytest"] in policy["shell_allowlist"]

    project = client.get("/api/projects/plain-project").json()
    assert project["phase"] == "build"
    assert {"kind": "repo_path", "value": str(workdir.resolve())} in project["bindings"]

    # v81-F10: the /repos deck answers with the same list as list_repos —
    # workon-bound dirs included, not just clones.
    listed = client.get("/api/repos").json()["repos"]
    assert {"name": workdir.name, "path": str(workdir.resolve()), "source": "workon"} in listed


def test_workon_on_an_existing_repo_binds_without_touching_the_tree(
    repo: Path, config: SupervisorConfig
) -> None:
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "scratch.txt").write_text("uncommitted\n")

    client = serve_client(config)
    preview = client.post("/api/workon/preview", json={"path": str(repo)}).json()
    assert preview["would_git_init"] is False
    assert preview["would_commit_baseline"] is False
    assert preview["git"]["dirty"] is True
    assert any("uncommitted" in warning for warning in preview["warnings"])

    result = client.post("/api/workon", json={"path": str(repo)}).json()
    assert result["git_initialized"] is False
    assert result["baseline_committed"] is False
    assert any("uncommitted" in warning for warning in result["warnings"])
    # Dirty-tree honesty: nothing was committed, nothing was cleaned.
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert "scratch.txt" in git(repo, "status", "--porcelain").stdout


def test_workon_refuses_the_store_missing_paths_and_too_broad_targets(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    client = serve_client(config)

    def refused(path: str) -> str:
        response = client.post("/api/workon/preview", json={"path": path})
        assert response.status_code == 400, path
        return str(response.json()["detail"])

    assert "store" in refused(str(config.home / "audit"))  # inside the supervisor store
    repos_root = config.home.parent / "repos"
    (repos_root / "cloned").mkdir(parents=True)
    assert "slug" in refused(str(repos_root / "cloned"))  # managed clones go by slug
    # v73-F11: a missing path gets the ONE shared story dispatch_run tells.
    assert "does not exist on this machine" in refused(str(tmp_path / "missing"))
    assert "relative" in refused("some/relative/dir")
    assert "too broad" in refused("/")
    assert "too broad" in refused("~")


def test_workon_project_id_collision_is_a_conflict(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    first = tmp_path / "api"
    second = tmp_path / "elsewhere" / "api"
    first.mkdir()
    second.mkdir(parents=True)

    client = serve_client(config)
    assert client.post("/api/workon", json={"path": str(first)}).status_code == 201
    # Same dir again is an update, not a conflict (v24-F4 idempotent setup).
    assert client.post("/api/workon", json={"path": str(first)}).status_code == 201
    collided = client.post("/api/workon/preview", json={"path": str(second)})
    assert collided.status_code == 409
    assert "already exists" in collided.json()["detail"]


def test_workon_is_a_confirm_carded_tool_on_both_faces(config: SupervisorConfig) -> None:
    """The chat tool and the deck command are the same verb behind the same
    confirmation: mutating for the model, command-tool for the operator."""
    assert "workon" in MUTATING_TOOL_NAMES
    assert "workon" in COMMAND_TOOL_NAMES

    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "workon:" in source
    assert '"/api/workon/preview"' in source
    assert 'await proposeCommand("workon", body, notes)' in source
    # The card states what will happen before anything runs.
    assert "preview.would_git_init" in source
    assert "preview.would_commit_baseline" in source


def test_workon_over_the_command_deck_executes_on_confirm(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    workdir = tmp_path / "deck-dir"
    workdir.mkdir()
    (workdir / "notes.md").write_text("hello\n")

    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    action_id = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "workon", "args": {"path": str(workdir)}},
    ).json()["action_id"]
    # Proposing is not executing.
    assert not (workdir / ".git").exists()

    confirmed = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm").json()
    assert confirmed["ok"] is True
    assert confirmed["result"]["baseline_committed"] is True
    assert (workdir / ".git").is_dir()
    assert confirmed["result"]["effective_policy"]["project"]["project_id"] == "deck-dir"
