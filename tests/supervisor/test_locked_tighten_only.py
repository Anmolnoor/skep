"""D7 (operator decision 2026-11-13): under a locked (approved) baseline,
set_operator_policy only tightens — a deny lands in any scope, an allow is
refused at proposal naming `skep policy approve`. Homes with no approved
baseline behave as before; kept card grants are untouched."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_write import PolicyWriteRejected
from skep.supervisor.serve.actions import remember_action_for_session, set_operator_policy_rule
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import policy_write_preview

REAPPROVE = "re-approve the baseline to allow this: `skep policy approve`"
ALLOWS = [("shell", "run", "git status"), ("network", "fetch", "docs.python.org"),
          ("filesystem", "read", "/opt/notes/**")]  # fmt: skip


def _home(tmp_path: Path, *, locked: bool) -> Path:
    home = tmp_path / "h"
    config = build_config(home, None)
    config.home.mkdir(parents=True)
    RunStore(config.db_path).close()
    if locked:
        assert (
            main(["--home", str(home), "policy", "approve", "--template", "builder", "--yes"]) == 0
        )
    return home


@pytest.mark.parametrize(("scope", "action", "pattern"), ALLOWS)
def test_an_allow_under_a_locked_baseline_is_refused_at_proposal(
    tmp_path: Path, scope: str, action: str, pattern: str
) -> None:
    config = build_config(_home(tmp_path, locked=True), None)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        with pytest.raises(HTTPException) as refused:
            set_operator_policy_rule(
                store, scope=scope, action=action, pattern=pattern, verdict="allow", holder=holder
            )
        assert refused.value.status_code == 400 and REAPPROVE in str(refused.value.detail)
        args = {"scope": scope, "action": action, "pattern": pattern, "verdict": "allow"}
        with pytest.raises(PolicyWriteRejected, match="skep policy approve"):
            policy_write_preview(store, holder, "set_operator_policy", args)  # no card
        # a deny still lands, in any scope
        assert (
            set_operator_policy_rule(
                store, scope=scope, action=action, pattern=pattern, verdict="deny", holder=holder
            )["verdict"]
            == "deny"
        )
        # kept card grants are unchanged by D7
        assert remember_action_for_session(
            store, tool="run_shell", args={"command": "ls -la"}, actor="op", tier="always"
        )
    finally:
        store.close()


def test_with_no_approved_baseline_an_allow_lands_as_before(tmp_path: Path) -> None:
    config = build_config(_home(tmp_path, locked=False), None)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        done = set_operator_policy_rule(
            store, scope="shell", action="run", pattern="git status", verdict="allow", holder=holder
        )
        assert done["matcher"] == "exact_argv"
    finally:
        store.close()
