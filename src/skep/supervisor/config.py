"""Supervisor configuration: one home directory, explicit knobs, no magic."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# v39-F3: the range is declared once, in the contract package (re-exported
# here so existing `from skep.supervisor import SUPPORTED_CONTRACT_RANGE`
# callers keep working).
from skep.worker_contract import SUPPORTED_CONTRACT_RANGE

from .policy import AutoApprovalRule

__all__ = ["SUPPORTED_CONTRACT_RANGE", "SupervisorConfig"]


@dataclass(frozen=True)
class SupervisorConfig:
    """Everything the dispatch pipeline needs, resolved once at the edge.

    ``worker_command`` is the argv prefix of the worker binary (e.g.
    ``("foundation", "run")``); the spawner appends the headless contract flags.
    ``env_baseline`` is the non-secret infrastructure floor every worker child
    gets (PATH to find binaries, HOME for the worker's own config); every other
    variable must be named in the task's ``permissions.env_allowlist`` (G2).
    """

    home: Path
    worker_command: tuple[str, ...]
    # D2: caste routing. ``worker_command`` is the default/`coding` worker; a caste
    # with its own implementation (e.g. `audit`) registers its argv here, and the
    # spawner picks the command by the task's ``worker_kind``. The boundary stays
    # the contract — each caste worker is spawned as a subprocess just the same.
    caste_worker_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    grace_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    poll_seconds: float = 0.05
    env_baseline: tuple[str, ...] = ("PATH", "HOME")
    contract_range: str = SUPPORTED_CONTRACT_RANGE
    # Q1: launch the worker under the host sandbox backend (Seatbelt on macOS,
    # Bubblewrap on Linux). ``sandbox_writable_extra`` adds writable roots a real
    # worker needs outside the workspace/results/temp; the gate never needs it.
    sandbox: bool = True
    sandbox_writable_extra: tuple[Path, ...] = ()
    # v44-F7: which backend wraps workers. "auto" = the native host backend
    # (Seatbelt/Bubblewrap); "podman" opts into the container backend, falling
    # back to native (with a logged warning) when podman can't run or can't
    # enforce the run's network shape.
    sandbox_backend: str = "auto"
    # D3: declarative auto-approval rules. Empty (default) = dormant — every run
    # waits for a human. A configured rule auto-applies the patch when its
    # conditions hold (verification + re-verification passed, no risk flags,
    # diff in scope). Mechanism is live in v2; goes active for real workloads in v3.
    auto_approval_rules: tuple[AutoApprovalRule, ...] = ()

    def command_for(self, worker_kind: str) -> tuple[str, ...]:
        """The worker argv for a caste — its registered command, else the default."""
        return self.caste_worker_commands.get(worker_kind, self.worker_command)

    @property
    def worktrees_root(self) -> Path:
        return self.home / "worktrees"

    @property
    def results_dir(self) -> Path:
        return self.home / "results"

    @property
    def audit_dir(self) -> Path:
        return self.home / "audit"

    @property
    def db_path(self) -> Path:
        return self.home / "supervisor.sqlite3"
