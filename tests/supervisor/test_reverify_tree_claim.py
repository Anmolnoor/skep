"""Phase 09 R4 (S3): a G10 worktree's live owner is a durable claim every
process's orphan sweep honours — not only the in-process ``_ACTIVE`` set.

The claim is an flock on ``<worktrees_root>/<name>.claim``: the kernel drops
it when its holder dies, so a sweep in ANOTHER process skips a live G10 tree
and removes a dead one's, with no pid-reuse guess."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig, reverify_run
from skep.supervisor.worktree import cleanup_orphans, create_worktree, tree_claim

from .test_pr_head_verification import _g10, _pr, _seed

_HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from skep.supervisor.worktree import tree_claim\n"
    "with tree_claim(Path(sys.argv[1]), sys.argv[2]) as held:\n"
    "    print('held' if held else 'not', flush=True)\n"
    "    sys.stdin.read()\n"
)


def test_a_sweep_in_another_process_skips_a_live_g10_tree_and_removes_a_dead_one(
    repo: Path, tmp_path: Path
) -> None:
    _seed(repo)
    root = tmp_path / "worktrees"
    live = create_worktree(repo, root, "reverify-live")
    dead = create_worktree(repo, root, "reverify-dead")
    (root / "reverify-dead.claim").write_text("4242\n")  # its owner died holding nothing
    owner = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root), "reverify-live"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None and owner.stdout.readline().strip() == "held"

        removed = cleanup_orphans(repo, root, keep=())  # no _ACTIVE, no keep set

        assert removed == [dead] and live.is_dir() and not dead.exists()
    finally:
        owner.communicate("")  # the owner exits: the kernel drops its claim
    assert cleanup_orphans(repo, root, keep=()) == [live] and not live.exists()
    assert not list(root.glob("*.claim"))  # a claim leaves nothing behind


def test_a_claim_held_elsewhere_is_never_handed_out_twice(tmp_path: Path) -> None:
    with (
        tree_claim(tmp_path, "reverify-x") as first,
        tree_claim(tmp_path, "reverify-x", wait=False) as second,
    ):
        assert first is True and second is False
    with tree_claim(tmp_path, "reverify-x", wait=False) as again:
        assert again is True


def test_every_g10_holds_its_tree_claim_for_the_whole_run(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one G10 creator (live dispatch, re-adoption, startup recovery and
    `--reverify` all reach it) claims ``reverify-<task_id>`` before the tree
    exists and keeps it until the tree is gone: a sweep mid-run, from any
    claim holder but this one, spares it."""
    reverify_module = sys.modules[reverify_run.__module__]
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    seen: list[tuple[str, bool, list[Path]]] = []
    real = reverify_module._needs_uv_priming

    def sweep_mid_run(worktree: Path, *args: Any) -> Any:
        removed = cleanup_orphans(repo, config.worktrees_root, keep=())
        seen.append((worktree.name, worktree.is_dir(), removed))
        return real(worktree, *args)

    monkeypatch.setattr(reverify_module, "_needs_uv_priming", sweep_mid_run)

    _outcome, record, task_id = _g10(repo, config, head)

    assert record is not None
    assert seen == [(f"reverify-{task_id}", True, [])]
    assert not (config.worktrees_root / f"reverify-{task_id}").exists()
    assert not (config.worktrees_root / f"reverify-{task_id}.claim").exists()
